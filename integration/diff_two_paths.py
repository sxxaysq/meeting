# -*- coding: utf-8 -*-
"""§4.4 双路 diff 验收驱动器。

顿悟平台经调研无 Dify DSL 导入入口（决策门结论见 acceptance_report.md），
Track A 平台链路无法在平台侧真实运行；本驱动器按"平台链路 = 无分页元数据的纯文本入口"
这一唯一已知差异点做等价复现：

- 路 1（src/cli.py 等价路径，PyMuPDF 逐页读取）：保留真实页码；
- 路 2（平台/Dify Document Extractor 等价路径）：同一份 PDF 用完全相同的逐页文本
  拼成纯文本 .txt 再进流水线（page_spans 为空 → evidence.page_start/page_end 恒 null）。

两路共用同一规范化/切分/对齐/质量门代码与同一模型，唯一结构性差异就是页码来源。
判定标准：逐字段 diff 的唯一允许差异 = evidence.page_start / evidence.page_end（路 2 为 null）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
M1_SRC = HERE.parent / "meeting-m2-work" / "M1_Extraction" / "src"
sys.path.insert(0, str(M1_SRC))

from llm_client import LLMClient, LLMConfig  # noqa: E402
from pipeline import run_pipeline  # noqa: E402

COMPARE_FIELDS = [
    "department", "work_section", "delivery_group", "project",
    "item_type", "assignee", "title", "content",
]


def pdf_to_naive_txt(pdf_path: Path, txt_path: Path) -> None:
    """与 document_reader._read_pdf 完全相同的逐页文本拼接，但不携带分页元数据。"""
    import pymupdf as fitz

    parts = []
    document = fitz.open(str(pdf_path))
    try:
        for page in document:
            text = page.get_text()
            if text and not text.endswith("\n"):
                text += "\n"
            parts.append(text)
    finally:
        document.close()
    txt_path.write_text("".join(parts), encoding="utf-8")


def run_once(input_path: Path, mode: str, max_block_chars: int):
    config = LLMConfig.from_env()
    client = LLMClient(config)
    return run_pipeline(
        str(input_path), client=client, max_block_chars=max_block_chars, mode=mode
    )


def diff_items(items_a: list, items_b: list) -> dict:
    """items_a = 有页码路径，items_b = 无页码路径。"""
    result = {
        "count_a": len(items_a),
        "count_b": len(items_b),
        "count_equal": len(items_a) == len(items_b),
        "field_diffs": [],
        "page_diff_only": [],
        "other_diffs": [],
    }
    for idx, (a, b) in enumerate(zip(items_a, items_b)):
        for field in COMPARE_FIELDS:
            if a.get(field) != b.get(field):
                result["field_diffs"].append({"index": idx, "field": field})
        ev_a, ev_b = a["evidence"], b["evidence"]
        for key in ("text", "start_char", "end_char", "exact_match"):
            if ev_a.get(key) != ev_b.get(key):
                result["other_diffs"].append({"index": idx, "evidence_key": key})
        pages_differ = (ev_a.get("page_start") != ev_b.get("page_start")) or (
            ev_a.get("page_end") != ev_b.get("page_end")
        )
        if pages_differ and ev_b.get("page_start") is None and ev_b.get("page_end") is None:
            result["page_diff_only"].append(idx)
    result["verdict"] = (
        "PASS：唯一差异为 evidence.page_start/page_end（路 2 恒 null）"
        if result["count_equal"] and not result["field_diffs"] and not result["other_diffs"]
        else "FAIL：存在允许范围外的差异，见 field_diffs / other_diffs"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 双路 diff 验收")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", default=str(HERE / "data" / "diff_run"))
    parser.add_argument("--mode", default="block")
    parser.add_argument("--max-block-chars", type=int, default=1200)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = Path(args.pdf)

    print("[diff] 路 1：PDF 路径（真实页码）...", file=sys.stderr)
    result_pdf = run_once(pdf_path, args.mode, args.max_block_chars)
    (out_dir / "items_pdf.json").write_text(
        json.dumps(result_pdf.items_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report_pdf.json").write_text(
        json.dumps(result_pdf.report(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    txt_path = out_dir / (pdf_path.stem + ".naive.txt")
    pdf_to_naive_txt(pdf_path, txt_path)
    print("[diff] 路 2：平台纯文本路径（页码 null）...", file=sys.stderr)
    result_txt = run_once(txt_path, args.mode, args.max_block_chars)
    (out_dir / "items_txt.json").write_text(
        json.dumps(result_txt.items_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report_txt.json").write_text(
        json.dumps(result_txt.report(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = diff_items(result_pdf.items, result_txt.items)
    summary["sample_pdf"] = str(pdf_path)
    summary["mode"] = args.mode
    summary["exact_match_pdf"] = result_pdf.report()["exact_match_rate"]
    summary["exact_match_txt"] = result_txt.report()["exact_match_rate"]
    (out_dir / "diff_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
