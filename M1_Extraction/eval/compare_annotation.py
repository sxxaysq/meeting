# -*- coding: utf-8 -*-
"""与人工标注 JSON 做回归对比。

标注文件 ``items.annotation_v3.merge_group.json`` 是 10 份会议拼接后的
一个 items 数组，没有文档字段；文档边界靠 evidence.start_char 回退来切分。

**不修改标注数据来迎合模型输出。**

对比口径
--------
M1 按需求"宁可拆多不可错合"，粒度天然比 merge_group 标注更细
（标注把一个项目下的 ①②③ 合成一条），所以同时报告两组指标：

* 严格匹配：predicted 与 gold 的 evidence 区间 IoU >= 阈值才算配对，
  用于字段准确率；
* 覆盖率：gold 区间被 predicted 区间覆盖的比例，用于衡量召回/遗漏，
  不惩罚合理拆分。

用法::

    python eval/compare_annotation.py \
        --annotation /home/yty/标注/items.annotation_v3.merge_group.json \
        --doc-index 0 --predicted out/2026-04-07.items.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FIELDS = ("department", "work_section", "delivery_group", "project", "item_type")


def split_documents(items):
    """标注是多份文档拼接的，start_char 回退处即为文档边界。"""
    docs = [[]]
    previous = -1
    for item in items:
        start = item["evidence"]["start_char"]
        if start < previous:
            docs.append([])
        docs[-1].append(item)
        previous = start
    return docs


def _span(item):
    evidence = item["evidence"]
    return evidence["start_char"], evidence["end_char"]


def _overlap(a, b):
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def _iou(a, b):
    inter = _overlap(a, b)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def match_items(gold, predicted, threshold=0.5):
    """贪心配对：按 IoU 从高到低，一对一。"""
    pairs = []
    for gi, g in enumerate(gold):
        for pi, p in enumerate(predicted):
            score = _iou(_span(g), _span(p))
            if score >= threshold:
                pairs.append((score, gi, pi))
    pairs.sort(reverse=True)
    used_g, used_p, matched = set(), set(), []
    for score, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        matched.append((gi, pi, score))
    return matched, used_g, used_p


def _norm(value):
    return (value or "").strip()


def _norm_lenient(value):
    """宽松口径：忽略标注保留的行尾冒号等排版差异。"""
    return _norm(value).rstrip("：:　 ").strip()


# 标注在"下一步工作要求"章节把整句话塞进了 work_section，
# 这类超长值是标注工具的产物而非真实板块名，单独统计以免掩盖真实准确率。
SECTION_LABEL_MAX_CHARS = 20


def coverage_stats(gold, predicted):
    """gold 区间被 predicted 覆盖的字符比例，衡量遗漏。"""
    covered_fully = 0
    partially = 0
    missed = []
    for g in gold:
        gs, ge = _span(g)
        length = max(1, ge - gs)
        covered = sum(_overlap((gs, ge), _span(p)) for p in predicted)
        ratio = min(1.0, covered / length)
        if ratio >= 0.9:
            covered_fully += 1
        elif ratio > 0:
            partially += 1
        else:
            missed.append(
                {
                    "project": g.get("project"),
                    "title": g.get("title"),
                    "span": [gs, ge],
                }
            )
    return covered_fully, partially, missed


def compare(gold, predicted, threshold=0.5):
    matched, used_g, used_p = match_items(gold, predicted, threshold)
    field_hits = {f: 0 for f in FIELDS}
    lenient_hits = {f: 0 for f in FIELDS}
    assignee_hits = 0
    mismatches = {f: [] for f in FIELDS}
    section_label_pairs = 0
    section_label_hits = 0

    for gi, pi, _score in matched:
        g, p = gold[gi], predicted[pi]
        for field in FIELDS:
            if _norm(g.get(field)) == _norm(p.get(field)):
                field_hits[field] += 1
            elif len(mismatches[field]) < 8:
                mismatches[field].append(
                    {"gold": g.get(field), "pred": p.get(field), "title": p.get("title")}
                )
            if _norm_lenient(g.get(field)) == _norm_lenient(p.get(field)):
                lenient_hits[field] += 1
        gold_section = _norm(g.get("work_section"))
        if len(gold_section) <= SECTION_LABEL_MAX_CHARS:
            section_label_pairs += 1
            if _norm_lenient(gold_section) == _norm_lenient(p.get("work_section")):
                section_label_hits += 1
        if sorted(g.get("assignee") or []) == sorted(p.get("assignee") or []):
            assignee_hits += 1

    total = max(1, len(matched))
    covered_fully, partially, missed = coverage_stats(gold, predicted)
    exact = sum(1 for p in predicted if p["evidence"]["exact_match"])

    return {
        "gold_items": len(gold),
        "predicted_items": len(predicted),
        "item_count_delta": len(predicted) - len(gold),
        "granularity_ratio": round(len(predicted) / max(1, len(gold)), 3),
        "matched_pairs": len(matched),
        "unmatched_gold_strict": len(gold) - len(used_g),
        "unmatched_predicted_strict": len(predicted) - len(used_p),
        "coverage": {
            "gold_fully_covered": covered_fully,
            "gold_partially_covered": partially,
            "gold_missed": len(missed),
            "gold_recall_full": round(covered_fully / max(1, len(gold)), 4),
            "missed_examples": missed[:10],
        },
        "field_accuracy_on_matched": {
            **{f: round(field_hits[f] / total, 4) for f in FIELDS},
            "assignee": round(assignee_hits / total, 4),
        },
        "field_accuracy_lenient": {
            f: round(lenient_hits[f] / total, 4) for f in FIELDS
        },
        "work_section_on_real_labels": {
            "pairs": section_label_pairs,
            "accuracy": round(section_label_hits / max(1, section_label_pairs), 4),
            "note": (
                "只统计 gold work_section 长度 <= {} 的配对。标注在“下一步工作要求”"
                "章节把整句话写进了 work_section，那部分是标注工具产物，"
                "不代表真实板块名。".format(SECTION_LABEL_MAX_CHARS)
            ),
        },
        "evidence_exact_match_rate": round(exact / max(1, len(predicted)), 4),
        "field_mismatch_examples": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 输出与人工标注对比")
    parser.add_argument("--annotation", required=True)
    parser.add_argument("--predicted", required=True, help="M1 输出的 items.json")
    parser.add_argument("--doc-index", type=int, required=True, help="标注中的文档序号，从 0 开始")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--output", "-o")
    args = parser.parse_args()

    annotation = json.loads(Path(args.annotation).read_text(encoding="utf-8"))["items"]
    documents = split_documents(annotation)
    if not 0 <= args.doc_index < len(documents):
        raise SystemExit(
            "doc-index 超出范围，标注共 {} 份文档".format(len(documents))
        )
    gold = documents[args.doc_index]
    predicted = json.loads(Path(args.predicted).read_text(encoding="utf-8"))["items"]

    report = compare(gold, predicted, args.threshold)
    report["doc_index"] = args.doc_index
    report["annotation_documents"] = len(documents)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
