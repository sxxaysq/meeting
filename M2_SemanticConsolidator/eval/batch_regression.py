# -*- coding: utf-8 -*-
"""M2 全量回归：对 10 份 M1 输出跑 M2 并汇总。

``--source-mode`` 必填且必须与 M1 输出目录的模式一致。
block 与 generic **必须分别跑、分别报告**。

方差提醒：Qwen/vLLM 即使 temperature=0，并发下仍有明显方差。
关键实验请 ``--workers 1``，或用 ``--repeat N`` 跑多次看中位数；
每次的原始结果都会落盘，不覆盖。

用法::

    python eval/batch_regression.py \\
        --m1-dir ../M1_Extraction/out_v2 --source-mode block \\
        --annotation /home/yty/标注/items.annotation_v3.merge_group.json \\
        --strict-annotation /home/yty/标注/items.annotation_v3.strict.json \\
        --text-dir /home/yty/m1_annotation/text \\
        --out-dir out_m2_block --workers 1
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate_m2 import evaluate  # noqa: E402
from gold_merge_groups import load_annotation  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from pipeline import run_pipeline  # noqa: E402
from project_normalizer import ProjectCatalog  # noqa: E402


def _text_for(text_dir: Optional[str], stem: str) -> Optional[str]:
    if not text_dir:
        return None
    path = Path(text_dir) / (stem + ".body.txt")
    return path.read_text(encoding="utf-8") if path.exists() else None


def _median(values: List[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 4) if clean else None


def run_once(args, run_index: int) -> Dict[str, object]:
    m1_files = sorted(Path(args.m1_dir).glob("*.items.json"))
    gold_docs = load_annotation(args.annotation)
    strict_docs = load_annotation(args.strict_annotation) if args.strict_annotation else None
    if len(m1_files) != len(gold_docs):
        print(
            "警告：M1 输出 {} 份，标注 {} 份，按顺序对齐前 {} 份".format(
                len(m1_files), len(gold_docs), min(len(m1_files), len(gold_docs))
            ),
            file=sys.stderr,
        )

    out_dir = Path(args.out_dir) / ("run{}".format(run_index) if args.repeat > 1 else ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    client = None if args.no_llm else LLMClient()
    # alias 跨会议累积，正是项目实体归一想要的效果
    catalog = ProjectCatalog(str(out_dir / "project_catalog.sqlite") if args.persist_catalog else None)
    rows = []

    for index, m1_file in enumerate(m1_files[: len(gold_docs)]):
        started = time.time()
        stem = m1_file.name.replace(".items.json", "")
        payload = json.loads(m1_file.read_text(encoding="utf-8"))
        result = run_pipeline(
            payload,
            source_mode=args.source_mode,
            client=client,
            catalog=catalog,
            normalized_text=_text_for(args.text_dir, stem),
            workers=args.workers,
            generate_titles=not args.no_title_llm,
        )
        (out_dir / (stem + ".m2.json")).write_text(
            json.dumps(result.payload(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / (stem + ".report.json")).write_text(
            json.dumps(result.report(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if result.review:
            (out_dir / (stem + ".review.json")).write_text(
                json.dumps(result.review, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        report = evaluate(
            result.payload(),
            payload["items"],
            gold_docs[index],
            strict_docs[index] if strict_docs else None,
        )
        (out_dir / (stem + ".eval.json")).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        rows.append({"doc": stem, "seconds": round(time.time() - started, 1), **report})
        print(
            "[{}/{}] {} {}s  m1={} m2={} merges={} P={} R={} over={} status={}".format(
                index + 1,
                len(gold_docs),
                stem,
                rows[-1]["seconds"],
                report["counts"]["m1_items"],
                report["counts"]["m2_items"],
                report["counts"]["merge_operations"],
                report["merge_project_consistent"]["precision"],
                report["merge_project_consistent"]["recall"],
                report["over_merge"]["hard_negative_violations"],
                report["validation"]["status"],
            ),
            file=sys.stderr,
        )

    def total(path: List[str]) -> int:
        value = 0
        for row in rows:
            node = row
            for key in path:
                node = node[key]
            value += node or 0
        return value

    # 池化（把 10 份的 pair 计数加总后再算比率），比逐份平均更能反映整体
    def pooled(view: str) -> Dict[str, object]:
        tp = total([view, "true_positive"])
        pred = total([view, "predicted_pairs"])
        gold = total([view, "gold_pairs"])
        precision = tp / pred if pred else None
        recall = tp / gold if gold else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision and recall
            else None
        )
        return {
            "predicted_pairs": pred,
            "gold_pairs": gold,
            "true_positive": tp,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": round(f1, 4) if f1 is not None else None,
        }

    predicted_pairs = total(["merge_full_annotation", "predicted_pairs"])
    summary = {
        "source_mode": args.source_mode,
        "run_index": run_index,
        "documents": len(rows),
        "m1_items": total(["counts", "m1_items"]),
        "m2_items": total(["counts", "m2_items"]),
        "gold_items": total(["counts", "gold_items"]),
        "merge_operations": total(["counts", "merge_operations"]),
        "merge_project_consistent": pooled("merge_project_consistent"),
        "merge_full_annotation": pooled("merge_full_annotation"),
        "over_merge": {
            "pairs": total(["over_merge", "pairs"]),
            "rate": round(total(["over_merge", "pairs"]) / predicted_pairs, 4)
            if predicted_pairs
            else None,
            "hard_negative_violations": total(["over_merge", "hard_negative_violations"]),
            "hard_negative_rate": round(
                total(["over_merge", "hard_negative_violations"]) / predicted_pairs, 4
            )
            if predicted_pairs
            else None,
        },
        "under_merge": {
            "pairs": total(["under_merge", "pairs"]),
            "rate": round(
                total(["under_merge", "pairs"])
                / max(1, total(["merge_project_consistent", "gold_pairs"])),
                4,
            ),
        },
        "coverage_m1": round(
            total(["coverage_m1", "gold_fully_covered"]) / max(1, total(["counts", "gold_items"])),
            4,
        ),
        "coverage_m2": round(
            total(["coverage_m2", "gold_fully_covered"]) / max(1, total(["counts", "gold_items"])),
            4,
        ),
        # 主指标：与 canonical 取名无关的实体归一质量
        "project_entity_m1": {
            "pair_accuracy": _median([r["project_entity_pairs_m1"]["pair_accuracy"] for r in rows]),
            "wrongly_linked_pairs": total(["project_entity_pairs_m1", "wrongly_linked_pairs"]),
            "wrongly_split_pairs": total(["project_entity_pairs_m1", "wrongly_split_pairs"]),
        },
        "project_entity_m2": {
            "pair_accuracy": _median([r["project_entity_pairs_m2"]["pair_accuracy"] for r in rows]),
            "wrongly_linked_pairs": total(["project_entity_pairs_m2", "wrongly_linked_pairs"]),
            "wrongly_split_pairs": total(["project_entity_pairs_m2", "wrongly_split_pairs"]),
        },
        # 次要口径：与标注 project 字符串精确相等，受标注命名不统一影响
        "project_string_accuracy_m1": _median([r["field_accuracy_m1"]["project"] for r in rows]),
        "project_string_accuracy_m2": _median([r["field_accuracy_m2"]["project"] for r in rows]),
        "schema_violations": total(["validation", "schema_violations"]),
        "review_rate": round(
            total(["validation", "review_items"]) / max(1, total(["counts", "m2_items"])), 4
        ),
        "status_counts": {
            status: sum(1 for r in rows if r["validation"]["status"] == status)
            for status in ("PASS", "REVIEW", "ERROR")
        },
        "total_seconds": round(sum(r["seconds"] for r in rows), 1),
        "llm": client.stats() if client else None,
        "per_document": rows,
    }
    (out_dir / "m2_regression_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="M2 全量回归")
    parser.add_argument("--m1-dir", required=True, help="M1 输出目录（*.items.json）")
    parser.add_argument("--source-mode", required=True, choices=("block", "generic"))
    parser.add_argument("--annotation", required=True)
    parser.add_argument("--strict-annotation")
    parser.add_argument("--text-dir", help="规范化文本目录（*.body.txt）")
    parser.add_argument("--out-dir", default="out_m2")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=1, help="重复次数，报中位数")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-title-llm", action="store_true")
    parser.add_argument("--persist-catalog", action="store_true")
    args = parser.parse_args()

    summaries = [run_once(args, i) for i in range(args.repeat)]
    if args.repeat > 1:
        aggregate = {
            "source_mode": args.source_mode,
            "runs": args.repeat,
            "median": {
                "merge_precision_project_consistent": _median(
                    [s["merge_project_consistent"]["precision"] for s in summaries]
                ),
                "merge_recall_project_consistent": _median(
                    [s["merge_project_consistent"]["recall"] for s in summaries]
                ),
                "over_merge_rate": _median([s["over_merge"]["rate"] for s in summaries]),
                "hard_negative_violations": _median(
                    [float(s["over_merge"]["hard_negative_violations"]) for s in summaries]
                ),
                "m2_items": _median([float(s["m2_items"]) for s in summaries]),
                "coverage_m2": _median([s["coverage_m2"] for s in summaries]),
            },
            "runs_detail": [
                {k: v for k, v in s.items() if k != "per_document"} for s in summaries
            ],
        }
        Path(args.out_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.out_dir) / "m2_repeat_summary.json").write_text(
            json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(aggregate["median"], ensure_ascii=False, indent=2))
        return 0

    print(
        json.dumps(
            {k: v for k, v in summaries[0].items() if k != "per_document"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
