# -*- coding: utf-8 -*-
"""M1 命令行入口。

    python src/cli.py extract 会议.pdf -o out/items.json
    python src/cli.py blocks  会议.pdf            # 只看结构切分，不调模型
    python src/cli.py text    会议.pdf            # 只看规范化文本与坐标系
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from document_reader import read_document  # noqa: E402
from llm_client import LLMClient, LLMConfig  # noqa: E402
from pipeline import blocks_only, run_pipeline  # noqa: E402
from text_normalizer import normalize  # noqa: E402


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_text(args) -> int:
    doc = normalize(read_document(args.input))
    if args.output:
        _write(Path(args.output), {"text": doc.text, "lines": len(doc.lines)})
    else:
        sys.stdout.write(doc.text)
    print(
        "\n[规范化] 字符数={} 条目行={} 页数={}".format(
            len(doc.text), len(doc.lines), len(doc.raw.page_spans) if doc.raw else 0
        ),
        file=sys.stderr,
    )
    return 0


def cmd_blocks(args) -> int:
    payload = blocks_only(args.input, max_block_chars=args.max_block_chars)
    if args.output:
        _write(Path(args.output), payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("[结构切分] blocks={}".format(len(payload["blocks"])), file=sys.stderr)
    return 0


def cmd_extract(args) -> int:
    config = LLMConfig.from_env()
    print(
        "[模型] base_url={} model={} transport={} num_ctx={}".format(
            config.base_url, config.model, config.transport, config.num_ctx
        ),
        file=sys.stderr,
    )
    client = LLMClient(config)

    def progress(done: int, total: int) -> None:
        print("  抽取 Block {}/{}".format(done, total), file=sys.stderr)

    result = run_pipeline(
        args.input,
        client=client,
        max_block_chars=args.max_block_chars,
        workers=args.workers,
        mode=args.mode,
        progress=progress if args.verbose else None,
    )

    output = Path(args.output) if args.output else Path("out") / (
        Path(args.input).stem + ".items.json"
    )
    report_path = output.with_suffix(".report.json")
    # 报告永远写出，Schema 失败时它就是排查依据
    _write(report_path, result.report())

    if result.schema_errors and not args.allow_invalid:
        print(
            "Schema 校验失败，未写出 items（诊断见 {}）：\n- {}".format(
                report_path, "\n- ".join(result.schema_errors[:20])
            ),
            file=sys.stderr,
        )
        return 2

    _write(output, result.items_payload())

    summary = result.report()
    print(
        "[完成] items={} blocks={} exact_match={:.1%} errors={} warnings={}".format(
            summary["item_count"],
            summary["block_count"],
            summary["exact_match_rate"],
            summary["quality_summary"].get("_errors", 0),
            summary["quality_summary"].get("_warnings", 0),
        ),
        file=sys.stderr,
    )
    print("[输出] {}\n[报告] {}".format(output, report_path), file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M1 Meeting Item Extraction")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler in (("text", cmd_text), ("blocks", cmd_blocks), ("extract", cmd_extract)):
        child = sub.add_parser(name)
        child.add_argument("input", help="会议 PDF / DOCX / TXT")
        child.add_argument("--output", "-o")
        child.set_defaults(handler=handler)
        if name != "text":
            child.add_argument("--max-block-chars", type=int, default=1200)
    extract = sub.choices["extract"]
    extract.add_argument("--workers", type=int, default=1, help="并发 Block 数")
    extract.add_argument("--verbose", "-v", action="store_true")
    extract.add_argument(
        "--mode",
        choices=("block", "generic"),
        default="block",
        help="block=结构分块抽取（默认）；generic=整篇泛用高召回抽取",
    )
    extract.add_argument(
        "--allow-invalid",
        action="store_true",
        help="Schema 不通过时仍然写出结果（默认失败退出，不静默修正）",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print("错误：{}".format(error), file=sys.stderr)
        sys.exit(1)
