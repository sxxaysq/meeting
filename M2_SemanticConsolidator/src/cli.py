# -*- coding: utf-8 -*-
"""M2 命令行。

``--source-mode`` **必填**，不从 JSON 内容猜来源模式——猜错会让 block 的强结构
约束被套到 generic 输出上（或者反过来），两种模式的假设互相污染。

用法::

    python src/cli.py consolidate out_v2/2026-04-07.items.json \\
        --source-mode block \\
        --normalized-text /home/yty/m1_annotation/text/2026-04-07.body.txt \\
        -o out_m2/2026-04-07.m2.json

不调模型、只看候选生成（排查召回用）::

    python src/cli.py candidates out_v2/2026-04-07.items.json --source-mode block
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import merge_candidate_retriever  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from models import ERROR, SourceItem  # noqa: E402
from pipeline import run_pipeline  # noqa: E402
from project_entity_judge import ProjectEntityJudge  # noqa: E402
from project_normalizer import ProjectCatalog  # noqa: E402
from text_utils import normalize_name  # noqa: E402


def _load_items(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise SystemExit("输入必须是 M1 的 {{'items': [...]}} 结构：{}".format(path))
    return payload


def _write(path: str, payload) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_consolidate(args) -> int:
    payload = _load_items(args.input)
    normalized_text = (
        Path(args.normalized_text).read_text(encoding="utf-8")
        if args.normalized_text
        else None
    )
    client = None if args.no_llm else LLMClient()
    catalog = ProjectCatalog(args.catalog)

    result = run_pipeline(
        payload,
        source_mode=args.source_mode,
        client=client,
        catalog=catalog,
        normalized_text=normalized_text,
        workers=args.workers,
        max_gap=args.max_gap,
        generate_titles=not args.no_title_llm,
        revalidate_pairs=args.revalidate_pairs,
    )

    if args.output:
        stem = Path(args.output).with_suffix("")
        if result.validation["status"] == ERROR and not args.allow_invalid:
            _write(str(stem) + ".report.json", result.report())
            print(
                "质量门 ERROR，未写出业务结果（--allow-invalid 可强制）：\n"
                + json.dumps(result.report(), ensure_ascii=False, indent=2),
                file=sys.stderr,
            )
            return 2
        _write(args.output, result.payload())
        _write(str(stem) + ".report.json", result.report())
        if result.review:
            _write(str(stem) + ".review.json", result.review)
        if args.sft_out:
            _write(args.sft_out, result.sft)

    summary = {
        "source_mode": result.source_mode,
        "source_items": result.stats["source_items"],
        "output_items": result.stats["output_items"],
        "merge_operations": result.stats["merge_operations"],
        "merged_clusters": result.stats["merged_clusters"],
        "validation": result.validation["status"],
        "issues": result.validation.get("issue_counts", {}),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_candidates(args) -> int:
    """只跑候选生成，不调模型。用来看召回是否合理。"""
    payload = _load_items(args.input)
    items = [SourceItem(index=i, raw=raw) for i, raw in enumerate(payload["items"])]
    project_of = {}
    if args.resolve_projects and not args.no_llm:
        judge = ProjectEntityJudge(client=LLMClient(), catalog=ProjectCatalog())
        entities, name_to_entity = judge.resolve(items)
        for item in items:
            project_of[item.index] = (
                name_to_entity.get(normalize_name(item.project or "")) if item.project else None
            )
    else:
        # 不归一时用原始项目名当实体键，纯粹为了看候选规模
        for item in items:
            project_of[item.index] = normalize_name(item.project) or None

    candidates = merge_candidate_retriever.generate(
        args.source_mode, items, project_of, max_gap=args.max_gap
    )
    rows = [
        {
            "left": c.left,
            "right": c.right,
            "left_title": items[c.left].title,
            "right_title": items[c.right].title,
            "signals": c.signals,
        }
        for c in candidates
    ]
    output = {"source_mode": args.source_mode, "items": len(items), "candidates": rows}
    if args.output:
        _write(args.output, output)
    print(
        json.dumps(
            {
                "source_mode": args.source_mode,
                "items": len(items),
                "candidate_pairs": len(rows),
                "pairs_per_item": round(2 * len(rows) / max(1, len(items)), 2),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_projects(args) -> int:
    """只跑项目实体归一，输出实体表。"""
    payload = _load_items(args.input)
    items = [SourceItem(index=i, raw=raw) for i, raw in enumerate(payload["items"])]
    client = None if args.no_llm else LLMClient()
    judge = ProjectEntityJudge(client=client, catalog=ProjectCatalog(args.catalog))
    entities, _ = judge.resolve(items)
    output = {
        "project_entities": [entity.to_dict() for entity in entities.values()],
        "uncertain": judge.uncertain,
        "llm_calls": judge.calls,
    }
    if args.output:
        _write(args.output, output)
    print(json.dumps({k: v for k, v in output.items() if k != "project_entities"},
                     ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M2 语义归并与校验")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("input", help="M1 输出的 items.json")
        sub.add_argument(
            "--source-mode",
            required=True,
            choices=("block", "generic"),
            help="M1 的来源模式。必填，不从内容猜。",
        )
        sub.add_argument("--output", "-o")
        sub.add_argument("--no-llm", action="store_true", help="不调模型（判定全部 UNCERTAIN）")
        sub.add_argument("--max-gap", type=int, help="候选生成的最大字符间隔")

    consolidate = subparsers.add_parser("consolidate", help="完整 M2 链路")
    add_common(consolidate)
    consolidate.add_argument(
        "--normalized-text",
        help="规范化文本（.body.txt）。给了才能重算合并后的包络 evidence。",
    )
    consolidate.add_argument("--catalog", help="项目 alias SQLite 路径，不给则只在内存里")
    consolidate.add_argument("--workers", type=int, default=1)
    consolidate.add_argument(
        "--no-title-llm", action="store_true", help="合并标题不调模型，直接用来源标题"
    )
    consolidate.add_argument(
        "--revalidate-pairs", action="store_true", help="两元素簇也走整簇复判"
    )
    consolidate.add_argument("--allow-invalid", action="store_true")
    consolidate.add_argument("--sft-out", help="导出 SFT 判定数据")
    consolidate.set_defaults(func=cmd_consolidate)

    candidates = subparsers.add_parser("candidates", help="只看候选生成，不调模型")
    add_common(candidates)
    candidates.add_argument("--resolve-projects", action="store_true")
    candidates.set_defaults(func=cmd_candidates)

    projects = subparsers.add_parser("projects", help="只跑项目实体归一")
    add_common(projects)
    projects.add_argument("--catalog")
    projects.set_defaults(func=cmd_projects)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
