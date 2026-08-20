# -*- coding: utf-8 -*-
"""单份 M2 结果的评估。

报告的指标（需求第十六节）::

    merge precision / recall / F1     分 full 与 project_consistent 两个视图（仅参照）
    over-merge rate + hard-negative 违例数（**主指标**）
    under-merge rate                  （仅参照，同上）
    project entity resolution         按对聚类算，与命名口径无关（**主指标**）
    最终 item 数量 / gold coverage    （不能比 M1 低）
    Schema violation / REVIEW rate

⚠️ **不要把 merge recall 当优化目标。** 详见 ``gold_merge_groups.py`` 顶部：
这份标注的合并语义是"源段落粒度"，54 个 project 同质组逐条核对后
全部是"同段落里的不同业务目标"，也就是需求明令禁止合并的那一类。
对着它提 recall 等于把 M2 训成 over-merge。

这份标注上**唯一可信**的 merge 质量指标是 ``hard_negative_violations``
（合并了 gold 侧 project 明确不同的两条），目标 0。

关于 project 指标的两种口径
---------------------------
``project_entity_pairs``（主）
    按对算聚类一致性：gold 认为同项目的两条，M2 是否也归到同一实体；
    gold 认为不同项目的两条，M2 是否保持不同。**与选哪个名字当 canonical 无关。**
``field_accuracy_*.project``（次）
    与标注 project 字符串精确相等的比例。这个口径有先天缺陷：
    标注的 ``project`` 是**逐条抄的原文写法**，不是统一的规范名——
    ``互联网收敛项目`` 的标注规范名是更长的 ``集团互联网收敛项目``，
    而 ``平庄煤业调运智能指挥中心建设项目`` 的标注规范名却是更短的 ``平庄煤业``。
    所以归一到任何一个固定形态都会与部分标注不符，该指标只能看趋势。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gold_merge_groups import (  # noqa: E402
    assign_to_gold,
    gold_pairs,
    hard_negative_pairs,
    merged_pairs_from_trace,
    project_homogeneous_gold,
)

FIELDS = ("department", "work_section", "delivery_group", "project", "item_type")


def _span(item: dict) -> Tuple[int, int]:
    evidence = item["evidence"]
    return int(evidence["start_char"]), int(evidence["end_char"])


def _overlap(a, b) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def prf(predicted: Set, gold: Set) -> Dict[str, float]:
    true_positive = len(predicted & gold)
    precision = true_positive / len(predicted) if predicted else None
    recall = true_positive / len(gold) if gold else None
    if precision is None or recall is None or precision + recall == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "predicted_pairs": len(predicted),
        "gold_pairs": len(gold),
        "true_positive": true_positive,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
    }


def coverage(gold: List[dict], predicted: List[dict]) -> Dict[str, float]:
    """gold 区间被预测区间覆盖 >= 90% 的比例。与 M1 evaluate 口径一致。"""
    full = 0
    for gold_item in gold:
        gs, ge = _span(gold_item)
        length = max(1, ge - gs)
        covered = sum(_overlap((gs, ge), _span(p)) for p in predicted)
        if min(1.0, covered / length) >= 0.9:
            full += 1
    return {
        "gold_items": len(gold),
        "gold_fully_covered": full,
        "gold_recall_full": round(full / max(1, len(gold)), 4),
    }


def field_accuracy(
    gold: List[dict], predicted: List[dict], threshold: float = 0.5
) -> Dict[str, Optional[float]]:
    """IoU 配对上的字段一致率。与 M1 的 compare_annotation 同口径，可直接对比。"""
    pairs = []
    for gi, gold_item in enumerate(gold):
        for pi, item in enumerate(predicted):
            a, b = _span(gold_item), _span(item)
            inter = _overlap(a, b)
            union = (a[1] - a[0]) + (b[1] - b[0]) - inter
            score = inter / union if union else 0.0
            if score >= threshold:
                pairs.append((score, gi, pi))
    pairs.sort(reverse=True)
    used_g, used_p, matched = set(), set(), []
    for score, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        matched.append((gi, pi))

    hits = {field: 0 for field in FIELDS}
    for gi, pi in matched:
        for field in FIELDS:
            if (gold[gi].get(field) or "").strip() == (predicted[pi].get(field) or "").strip():
                hits[field] += 1
    total = len(matched)
    return {
        "matched_pairs": total,
        **{
            field: (round(hits[field] / total, 4) if total else None) for field in FIELDS
        },
    }


def project_entity_pairs(
    source_items: List[dict],
    gold: List[dict],
    assignment: Dict[int, int],
    entity_of: Dict[int, Optional[str]],
) -> Dict[str, object]:
    """按对衡量项目实体归一，**与 canonical 取名无关**。

    对于每两条都有 gold project 的来源 Item：
    * gold 同项目 → M2 应归到同一实体（正例）；
    * gold 不同项目 → M2 应保持不同实体（负例）。

    这样 ``互联网收敛项目`` 归一成 ``集团互联网收敛项目`` 还是反过来都算对，
    只要两条被认成同一个实体。M1 baseline 用原始 project 字符串充当实体键。
    """
    indexes = [
        i
        for i in sorted(assignment)
        if (gold[assignment[i]].get("project") or "").strip()
        and entity_of.get(i) is not None
    ]
    tp = fp = tn = fn = 0
    for a in range(len(indexes)):
        for b in range(a + 1, len(indexes)):
            i, j = indexes[a], indexes[b]
            gold_same = (gold[assignment[i]]["project"] or "").strip() == (
                gold[assignment[j]]["project"] or ""
            ).strip()
            pred_same = entity_of[i] == entity_of[j]
            if gold_same and pred_same:
                tp += 1
            elif gold_same:
                fn += 1
            elif pred_same:
                fp += 1
            else:
                tn += 1
    total = tp + fp + tn + fn
    return {
        "comparable_items": len(indexes),
        "pairs": total,
        "same_entity_precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "same_entity_recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "pair_accuracy": round((tp + tn) / total, 4) if total else None,
        "wrongly_linked_pairs": fp,
        "wrongly_split_pairs": fn,
    }


def _entity_keys(m2_payload: dict, m1_count: int) -> Dict[int, Optional[str]]:
    """来源 Item 下标 → M2 归一后的项目实体键。"""
    keys: Dict[int, Optional[str]] = {}
    items = m2_payload["items"]
    for entry in m2_payload["merge_trace"]:
        item = items[entry["item_index"]]
        key = entry.get("project_entity_id") or (item.get("project") or "").strip() or None
        for source in entry["source_indexes"]:
            keys[source] = key
    return keys


def _baseline_entity_keys(m1_items: List[dict]) -> Dict[int, Optional[str]]:
    """M1 baseline：不归一，原始 project 字符串就是实体键。"""
    return {
        i: ((item.get("project") or "").strip() or None) for i, item in enumerate(m1_items)
    }


def evaluate(
    m2_payload: dict,
    m1_items: List[dict],
    gold: List[dict],
    strict_gold: Optional[List[dict]] = None,
) -> Dict[str, object]:
    items = m2_payload["items"]
    trace = m2_payload["merge_trace"]
    validation = m2_payload.get("validation") or {}

    assignment = assign_to_gold(m1_items, gold)
    predicted_pairs = merged_pairs_from_trace(trace)

    full_gold_pairs = gold_pairs(assignment)
    homogeneous = (
        project_homogeneous_gold(gold, strict_gold) if strict_gold is not None else None
    )
    consistent_gold_pairs = (
        gold_pairs(assignment, homogeneous) if homogeneous is not None else set()
    )
    negatives = hard_negative_pairs(m1_items, gold, assignment)

    over_merge = predicted_pairs - full_gold_pairs
    under_merge = consistent_gold_pairs - predicted_pairs
    violations = predicted_pairs & negatives

    issues = validation.get("issues") or []
    review_items = {
        issue["item_index"]
        for issue in issues
        if issue.get("level") == "review" and issue.get("item_index") is not None
    }

    return {
        "counts": {
            "m1_items": len(m1_items),
            "m2_items": len(items),
            "gold_items": len(gold),
            "merge_operations": len(m1_items) - len(items),
            "granularity_ratio_m1": round(len(m1_items) / max(1, len(gold)), 3),
            "granularity_ratio_m2": round(len(items) / max(1, len(gold)), 3),
        },
        "_note": (
            "merge_* 两个视图仅供参照：标注的合并语义是源段落粒度，"
            "含需求禁止的同项目跨子系统合并。主指标看 over_merge.hard_negative_violations "
            "与 project_entity_pairs。"
        ),
        "merge_project_consistent": prf(predicted_pairs, consistent_gold_pairs),
        "merge_full_annotation": prf(predicted_pairs, full_gold_pairs),
        "project_entity_pairs_m2": project_entity_pairs(
            m1_items, gold, assignment, _entity_keys(m2_payload, len(m1_items))
        ),
        "project_entity_pairs_m1": project_entity_pairs(
            m1_items, gold, assignment, _baseline_entity_keys(m1_items)
        ),
        "over_merge": {
            "pairs": len(over_merge),
            "rate": round(len(over_merge) / len(predicted_pairs), 4)
            if predicted_pairs
            else None,
            "hard_negative_violations": len(violations),
            "hard_negative_rate": round(len(violations) / len(predicted_pairs), 4)
            if predicted_pairs
            else None,
        },
        "under_merge": {
            "pairs": len(under_merge),
            "rate": round(len(under_merge) / len(consistent_gold_pairs), 4)
            if consistent_gold_pairs
            else None,
        },
        "coverage_m2": coverage(gold, items),
        "coverage_m1": coverage(gold, m1_items),
        "field_accuracy_m2": field_accuracy(gold, items),
        "field_accuracy_m1": field_accuracy(gold, m1_items),
        "validation": {
            "status": validation.get("status"),
            "schema_violations": sum(
                1 for issue in issues if issue.get("code") == "SCHEMA_VIOLATION"
            ),
            "review_items": len(review_items),
            "review_rate": round(len(review_items) / max(1, len(items)), 4),
            "issue_counts": validation.get("issue_counts", {}),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="M2 单份评估")
    parser.add_argument("--m2", required=True, help="M2 输出 json")
    parser.add_argument("--m1", required=True, help="M1 输入 items.json")
    parser.add_argument("--annotation", required=True)
    parser.add_argument("--strict-annotation", help="未合并版标注，用于推导 project 同质组")
    parser.add_argument("--doc-index", type=int, required=True)
    parser.add_argument("--output", "-o")
    args = parser.parse_args()

    from gold_merge_groups import load_annotation

    gold_docs = load_annotation(args.annotation)
    strict_docs = load_annotation(args.strict_annotation) if args.strict_annotation else None
    m2_payload = json.loads(Path(args.m2).read_text(encoding="utf-8"))
    m1_items = json.loads(Path(args.m1).read_text(encoding="utf-8"))["items"]

    report = evaluate(
        m2_payload,
        m1_items,
        gold_docs[args.doc_index],
        strict_docs[args.doc_index] if strict_docs else None,
    )
    report["doc_index"] = args.doc_index
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
