# -*- coding: utf-8 -*-
"""合并后的字段生成。**禁止凭空增加事实。**

各字段规则（对应需求第十节）::

    content          只由 source items 的已有内容按原文顺序拼接，去完全重复
    title            允许 LLM 生成，但必须通过 grounding 校验，不过就退回来源标题
    assignee         来源去重并集，不新增
    department       结构一致取该值；明确冲突 → REVIEW
    delivery_group   同上
    work_section     block 同上；generic 允许 null 与非 null 合并取非 null
    project          取 Project Entity Resolver 的 canonical
    item_type        多数决，平局按"像任务"优先级；冲突写告警
    evidence         程序计算，绝不让 LLM 猜字符坐标

evidence 两种模式::

    bounding_span   有规范化文本时，取 [min_start, max_end] 的**字面切片**。
                    text 就是原文该区间，exact_match 由切片一致性验证得出。
    primary_source  没有规范化文本时，原样沿用首条来源的 evidence，不做任何拼接。

无论哪种模式，全部来源 evidence 都保留进 provenance。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models import (
    ITEM_TYPE_PRIORITY,
    ITEM_TYPES,
    Cluster,
    Issue,
    LEVEL_REVIEW,
    MergeTraceEntry,
    SourceItem,
)
from text_utils import dedupe_preserve_order, grounding_ratio

# LLM 生成的 title 至少要有这个比例的 2-gram 能在来源内容里找到，
# 否则视为凭空发挥，退回来源标题。
#
# 0.75 是实测定的：合法概括也会因为跨过原文标点而丢 bigram
# （"顶面线管安装" vs 原文 "顶面线管、桥架安装" 丢掉 "管安"，只有 0.8），
# 定到 0.85 会把正确标题也打回去。凭空发挥的标题实测在 0.5 以下，区分度足够。
TITLE_GROUNDING_MIN = 0.75
TITLE_MAX_CHARS = 60

TITLE_SYSTEM = """你是会议纪要归并结果的标题生成器。
给你若干条属于**同一个业务事项**的 Item，以及它们合并后的完整内容。
请生成一个简洁标题概括这个业务事项。

硬性要求：
1. 标题里出现的每一个具体名词（系统名、项目名、部门、交付物）都必须在给定内容里出现过；
2. 不得引入内容里没有的时间、比例、责任人、结论；
3. 不超过 30 个汉字；
4. 只输出 JSON：{"title": "..."}，不要任何解释。"""


def _non_empty(values: List[Optional[str]]) -> List[str]:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def merge_structural_field(
    sources: List[SourceItem], field: str, generic: bool
) -> Tuple[Optional[str], Optional[str]]:
    """返回 (取值, 冲突说明)。冲突时取值为 None，由调用方决定进 REVIEW。"""
    values = dedupe_preserve_order(_non_empty([s.get(field) for s in sources]))
    if not values:
        return None, None
    if len(values) == 1:
        # generic 下 null 与非 null 合并取有依据的非 null 值；block 同样成立
        return values[0], None
    return None, "{} 存在冲突：{}".format(field, " / ".join(values[:4]))


def merge_item_type(sources: List[SourceItem]) -> Tuple[str, Optional[str]]:
    counts: Dict[str, int] = {}
    for source in sources:
        value = source.get("item_type")
        if value in ITEM_TYPES:
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return "NON_TASK_ITEM", "来源均无合法 item_type"
    best = max(counts.items(), key=lambda kv: (kv[1], -ITEM_TYPE_PRIORITY[kv[0]]))
    conflict = None
    if len(counts) > 1:
        conflict = "item_type 不一致：{}".format(
            "，".join("{}×{}".format(k, v) for k, v in sorted(counts.items()))
        )
    return best[0], conflict


def merge_content(sources: List[SourceItem]) -> str:
    """按原文顺序拼接，去掉完全重复的段落。不改写、不概括。"""
    ordered = sorted(sources, key=lambda s: s.span)
    parts: List[str] = []
    seen = set()
    for source in ordered:
        text = source.content.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return "\n".join(parts)


def merge_assignee(sources: List[SourceItem]) -> List[str]:
    ordered = sorted(sources, key=lambda s: s.span)
    return dedupe_preserve_order([a for source in ordered for a in source.assignees])


def generate_title(
    sources: List[SourceItem], content: str, client=None
) -> Tuple[str, str]:
    """返回 (title, 来源标记)。生成失败或不接地气一律退回来源标题。"""
    ordered = sorted(sources, key=lambda s: s.span)
    fallback = ordered[0].title.strip() or ordered[0].content.strip()[:TITLE_MAX_CHARS]
    if client is None:
        return fallback[:TITLE_MAX_CHARS], "source"

    payload = {
        "source_titles": [s.title for s in ordered],
        "merged_content": content[:1500],
    }
    result = client.complete_json(
        TITLE_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=2)
    )
    if not isinstance(result, dict):
        return fallback[:TITLE_MAX_CHARS], "source"
    title = result.get("title")
    if not isinstance(title, str) or not title.strip():
        return fallback[:TITLE_MAX_CHARS], "source"
    title = title.strip()[:TITLE_MAX_CHARS]
    grounding = grounding_ratio(title, [content, *(s.title for s in ordered)])
    if grounding < TITLE_GROUNDING_MIN:
        return fallback[:TITLE_MAX_CHARS], "source_grounding_failed"
    return title, "llm"


def merge_evidence(
    sources: List[SourceItem], normalized_text: Optional[str]
) -> Tuple[Dict[str, object], str, bool]:
    """返回 (evidence, 模式, 是否连续)。字符坐标一律由程序计算。"""
    ordered = sorted(sources, key=lambda s: s.span)
    if len(ordered) == 1:
        return dict(ordered[0].get("evidence") or {}), "single", True

    start = min(s.span[0] for s in ordered)
    end = max(s.span[1] for s in ordered)
    # 连续性：来源区间之间的空隙里只有空白字符才算真正连续
    contiguous = True
    if normalized_text is not None:
        cursor = ordered[0].span[1]
        for source in ordered[1:]:
            gap_start, gap_end = cursor, source.span[0]
            if gap_end > gap_start and normalized_text[gap_start:gap_end].strip():
                contiguous = False
            cursor = max(cursor, source.span[1])
    else:
        contiguous = all(
            ordered[i].span[1] >= ordered[i + 1].span[0] for i in range(len(ordered) - 1)
        )

    pages = [
        (s.get("evidence") or {}).get("page_start") for s in ordered
    ] + [(s.get("evidence") or {}).get("page_end") for s in ordered]
    pages = [p for p in pages if isinstance(p, int)]

    if normalized_text is not None and 0 <= start < end <= len(normalized_text):
        text = normalized_text[start:end]
        # exact_match 由切片自身验证：text 就是原文该区间，不是拼接产物
        return (
            {
                "text": text,
                "page_start": min(pages) if pages else None,
                "page_end": max(pages) if pages else None,
                "start_char": start,
                "end_char": end,
                "exact_match": normalized_text[start:end] == text,
            },
            "bounding_span",
            contiguous,
        )

    # 没有规范化文本：原样沿用首条来源，绝不把拼接文字伪装成 exact_match
    primary = dict(ordered[0].get("evidence") or {})
    return primary, "primary_source", contiguous


def build_item(
    cluster: Cluster,
    by_index: Dict[int, SourceItem],
    canonical_project: Optional[str],
    project_entity_id: Optional[str],
    source_mode: str,
    normalized_text: Optional[str],
    output_index: int,
    title_client=None,
) -> Tuple[Dict[str, object], MergeTraceEntry, List[Issue]]:
    sources = [by_index[i] for i in cluster.members]
    generic = source_mode == "generic"
    issues: List[Issue] = []
    notes: List[str] = []

    if len(sources) == 1:
        source = sources[0]
        item = {
            "department": source.get("department"),
            "work_section": source.get("work_section"),
            "delivery_group": source.get("delivery_group"),
            "project": canonical_project if canonical_project else source.project,
            "item_type": source.get("item_type"),
            "assignee": source.assignees,
            "title": source.title,
            "content": source.content,
            "evidence": dict(source.get("evidence") or {}),
        }
        trace = MergeTraceEntry(
            item_index=output_index,
            source_indexes=[source.index],
            merged=False,
            evidence_mode="single",
            contiguous=True,
            source_evidence=[dict(source.get("evidence") or {})],
            project_entity_id=project_entity_id,
            title_source="source",
        )
        return item, trace, issues

    department, dept_conflict = merge_structural_field(sources, "department", generic)
    delivery, delivery_conflict = merge_structural_field(sources, "delivery_group", generic)
    section, section_conflict = merge_structural_field(sources, "work_section", generic)
    item_type, type_conflict = merge_item_type(sources)

    content = merge_content(sources)
    title, title_source = generate_title(sources, content, title_client)
    assignee = merge_assignee(sources)
    evidence, evidence_mode, contiguous = merge_evidence(sources, normalized_text)

    for conflict, code in (
        (dept_conflict, "DEPARTMENT_CONFLICT"),
        (delivery_conflict, "DELIVERY_GROUP_CONFLICT"),
    ):
        if conflict:
            issues.append(
                Issue(code, LEVEL_REVIEW, conflict, output_index, {"cluster": cluster.members})
            )
    if section_conflict:
        # generic 下 work_section 本就不可信，降级为提示而不是 REVIEW 依据
        issues.append(
            Issue(
                "WORK_SECTION_CONFLICT",
                "warning" if generic else LEVEL_REVIEW,
                section_conflict,
                output_index,
                {"cluster": cluster.members},
            )
        )
    if type_conflict:
        issues.append(
            Issue("ITEM_TYPE_CONFLICT", LEVEL_REVIEW, type_conflict, output_index)
        )
    if not contiguous:
        notes.append("来源 evidence 不连续，start/end 是包络区间")
    if title_source == "source_grounding_failed":
        notes.append("LLM 标题未通过 grounding 校验，已退回来源标题")

    item = {
        "department": department,
        "work_section": section,
        "delivery_group": delivery,
        "project": canonical_project,
        "item_type": item_type,
        "assignee": assignee,
        "title": title,
        "content": content,
        "evidence": evidence,
    }
    trace = MergeTraceEntry(
        item_index=output_index,
        source_indexes=list(cluster.members),
        merged=True,
        evidence_mode=evidence_mode,
        contiguous=contiguous,
        source_evidence=[dict(s.get("evidence") or {}) for s in sources],
        project_entity_id=project_entity_id,
        title_source=title_source,
        notes=notes,
    )
    return item, trace, issues
