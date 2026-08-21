# -*- coding: utf-8 -*-
"""对整套会议 PDF 跑 M1 并与人工标注做批量回归。

标注文件是 10 份会议按时间顺序拼接的一个 items 数组，
所以 PDF 也按会议日期排序后与 doc-index 一一对应。

用法::

    python eval/batch_regression.py \
        --pdf-dir /home/yty/数据集/原始数据 \
        --annotation /home/yty/标注/items.annotation_v3.merge_group.json \
        --out-dir out36
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_annotation import compare, split_documents  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from pipeline import run_pipeline  # noqa: E402

DATE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")
FIELDS = ("department", "work_section", "delivery_group", "project", "item_type", "assignee")


def meeting_date(path: Path):
    match = DATE.search(path.name)
    if not match:
        return (9999, 99, 99)
    return tuple(int(g) for g in match.groups())


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 批量回归")
    parser.add_argument("--pdf-dir", required=True)
    parser.add_argument("--annotation", required=True)
    parser.add_argument("--out-dir", default="out_batch")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--max-block-chars", type=int, default=1200)
    parser.add_argument(
        "--mode",
        choices=("block", "generic"),
        default="block",
        help="block=结构分块抽取；generic=整篇泛用高召回抽取",
    )
    args = parser.parse_args()

    pdfs = sorted(Path(args.pdf_dir).glob("*.pdf"), key=meeting_date)
    documents = split_documents(
        json.loads(Path(args.annotation).read_text(encoding="utf-8"))["items"]
    )
    if len(pdfs) != len(documents):
        print(
            "警告：PDF {} 份，标注 {} 份，按顺序对齐前 {} 份".format(
                len(pdfs), len(documents), min(len(pdfs), len(documents))
            ),
            file=sys.stderr,
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = LLMClient()
    rows = []

    for index, pdf in enumerate(pdfs[: len(documents)]):
        started = time.time()
        result = run_pipeline(
            pdf,
            client=client,
            max_block_chars=args.max_block_chars,
            workers=args.workers,
            mode=args.mode,
        )
        stem = "-".join("%02d" % v for v in meeting_date(pdf))
        (out_dir / (stem + ".items.json")).write_text(
            json.dumps(result.items_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out_dir / (stem + ".report.json")).write_text(
            json.dumps(result.report(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report = compare(documents[index], result.items)
        (out_dir / (stem + ".compare.json")).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        rows.append(
            {
                "doc_index": index,
                "file": pdf.name,
                "seconds": round(time.time() - started, 1),
                "mode": args.mode,
                "blocks": len(result.blocks),
                "gold": report["gold_items"],
                "pred": report["predicted_items"],
                "matched": report["matched_pairs"],
                "coverage": report["coverage"]["gold_recall_full"],
                "exact_match": report["evidence_exact_match_rate"],
                "errors": result.report()["quality_summary"].get("_errors", 0),
                "warnings": result.report()["quality_summary"].get("_warnings", 0),
                "schema_errors": len(result.schema_errors),
                **{f: report["field_accuracy_on_matched"][f] for f in FIELDS},
                "work_section_real": report["work_section_on_real_labels"]["accuracy"],
            }
        )
        print(
            "[{}/{}] {} {}s blocks={} gold={} pred={} cover={:.3f} exact={:.3f}".format(
                index + 1, len(documents), pdf.name, rows[-1]["seconds"],
                rows[-1]["blocks"], rows[-1]["gold"], rows[-1]["pred"],
                rows[-1]["coverage"], rows[-1]["exact_match"],
            ),
            file=sys.stderr,
        )

    def weighted(field, weight_key):
        total = sum(r[weight_key] for r in rows) or 1
        return round(sum(r[field] * r[weight_key] for r in rows) / total, 4)

    summary = {
        "mode": args.mode,
        "documents": len(rows),
        "total_gold": sum(r["gold"] for r in rows),
        "total_pred": sum(r["pred"] for r in rows),
        "total_matched": sum(r["matched"] for r in rows),
        "total_seconds": round(sum(r["seconds"] for r in rows), 1),
        "total_errors": sum(r["errors"] for r in rows),
        "total_schema_errors": sum(r["schema_errors"] for r in rows),
        "granularity_ratio": round(
            sum(r["pred"] for r in rows) / max(1, sum(r["gold"] for r in rows)), 3
        ),
        "coverage_weighted": weighted("coverage", "gold"),
        "exact_match_weighted": weighted("exact_match", "pred"),
        "field_accuracy_weighted": {f: weighted(f, "matched") for f in FIELDS},
        "work_section_real_weighted": weighted("work_section_real", "matched"),
        "llm": client.stats(),
        "per_document": rows,
    }
    (out_dir / "regression_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "per_document"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
