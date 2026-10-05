# -*- coding: utf-8 -*-
"""M2 质量门（需求第十二节）。

七类检查::

    1 Schema              字段类型与枚举合法          → ERROR
    2 Grounding           M2 新产生的内容可追溯        → ERROR
    3 Over-Merge          不同业务目标被合成一条       → REVIEW（最重要的错误类型）
    4 Under-Merge         归并后仍有高度疑似重复       → REVIEW
    5 Project Conflict    项目实体判断冲突或歧义       → REVIEW
    6 Structure Conflict  block 下强结构字段冲突       → REVIEW
    7 Generic Compat      generic 缺 work_section/project 不算错误

只报不改。最终状态 PASS / REVIEW / ERROR。
**不引入没有业务意义的浮点 confidence。**
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from models import (
    ERROR,
    Issue,
    LEVEL_ERROR,
    LEVEL_REVIEW,
    LEVEL_WARNING,
    MergeTraceEntry,
    PASS,
    REVIEW,
    SourceItem,
)
from text_utils import grounding_ratio, jaccard, normalize_name, overlap_coefficient

# 归并后仍然高度相似的两条 → 疑似漏合。门槛高是刻意的，见 check_under_merge。
UNDER_MERGE_SIMILARITY = 0.85
# title 的可追溯下限。必须 <= merger.TITLE_GROUNDING_MIN，
# 否则 merger 已经放行的标题会在质量门被判 ERROR。
GROUNDING_MIN = 0.70
# 合并簇内两两语义重合度低于此值 → 疑似错合
OVER_MERGE_MIN_COHESION = 0.06


def _text_pool(sources: List[SourceItem]) -> List[str]:
    pool: List[str] = []
    for source in sources:
        pool.append(source.title)
        pool.append(source.content)
        pool.append(str((source.get("evidence") or {}).get("text") or ""))
    return pool


def check_grounding(
    items: List[Dict[str, Any]],
    trace: List[MergeTraceEntry],
    by_index: Dict[int, SourceItem],
    alias_pool: set,
) -> List[Issue]:
    issues: List[Issue] = []
    for entry in trace:
        if not (0 <= entry.item_index < len(items)):
            continue
        item = items[entry.item_index]
        sources = [by_index[i] for i in entry.source_indexes if i in by_index]
        if not sources:
            issues.append(
                Issue(
                    "PROVENANCE_MISSING",
                    LEVEL_ERROR,
                    "输出 Item 找不到任何来源 Item",
                    entry.item_index,
                )
            )
            continue
        pool = _text_pool(sources)

        title_score = grounding_ratio(str(item.get("title") or ""), pool)
        if title_score < GROUNDING_MIN:
            issues.append(
                Issue(
                    "TITLE_NOT_GROUNDED",
                    LEVEL_ERROR,
                    "title 有 {:.0%} 的片段无法在来源中找到".format(1 - title_score),
                    entry.item_index,
                    {"title": item.get("title")},
                )
            )

        # content 必须完全由来源内容拼接而来
        source_contents = {source.content.strip() for source in sources}
        parts = [p for p in str(item.get("content") or "").split("\n") if p.strip()]
        unknown = [p for p in parts if p.strip() not in source_contents]
        if unknown:
            issues.append(
                Issue(
                    "CONTENT_NOT_GROUNDED",
                    LEVEL_ERROR,
                    "content 出现了不属于任何来源 Item 的片段",
                    entry.item_index,
                    {"unknown": unknown[:2]},
                )
            )

        source_assignees = {a for source in sources for a in source.assignees}
        extra = [a for a in (item.get("assignee") or []) if a not in source_assignees]
        if extra:
            issues.append(
                Issue(
                    "ASSIGNEE_NOT_GROUNDED",
                    LEVEL_ERROR,
                    "assignee 出现了来源中没有的人员",
                    entry.item_index,
                    {"extra": extra},
                )
            )

        project = item.get("project")
        if isinstance(project, str) and project.strip():
            source_projects = {normalize_name(s.project) for s in sources if s.project}
            if normalize_name(project) not in source_projects and normalize_name(
                project
            ) not in alias_pool:
                issues.append(
                    Issue(
                        "PROJECT_NOT_GROUNDED",
                        LEVEL_ERROR,
                        "project 既不在来源里，也不在已确认的 alias 里",
                        entry.item_index,
                        {"project": project},
                    )
                )
    return issues


def check_over_merge(
    items: List[Dict[str, Any]],
    trace: List[MergeTraceEntry],
    by_index: Dict[int, SourceItem],
    source_mode: str,
    project_mapping: Optional[Dict[str, str]] = None,
) -> List[Issue]:
    """最重要的一类：不同业务目标被合成一条。"""
    issues: List[Issue] = []
    for entry in trace:
        if not entry.merged:
            continue
        sources = [by_index[i] for i in entry.source_indexes if i in by_index]
        if len(sources) < 2:
            continue

        # 结构字段在簇内出现明确冲突
        for field, code in (
            ("department", "OVER_MERGE_DEPARTMENT"),
            ("delivery_group", "OVER_MERGE_DELIVERY_GROUP"),
        ):
            values = {
                str(s.get(field)).strip()
                for s in sources
                if isinstance(s.get(field), str) and str(s.get(field)).strip()
            }
            if len(values) > 1:
                issues.append(
                    Issue(
                        code,
                        LEVEL_REVIEW,
                        "合并簇内 {} 不一致：{}".format(field, " / ".join(sorted(values))),
                        entry.item_index,
                        {"sources": entry.source_indexes},
                    )
                )

        # 来源项目名归一前就分属不同实体（Resolver 已判 DIFFERENT）
        projects = {normalize_name(s.project) for s in sources if s.project}
        resolved = {(project_mapping or {}).get(p) for p in projects}
        if len(projects) > 1 and (None in resolved or len(resolved) != 1):
            issues.append(
                Issue(
                    "OVER_MERGE_PROJECT",
                    LEVEL_REVIEW,
                    "合并簇内存在未归一的多个项目称谓",
                    entry.item_index,
                    {"projects": sorted(p for p in projects if p)},
                )
            )

        # 内聚度：簇内两两语义重合都很低 → 很可能只是位置相邻被误合
        cohesion = _min_pairwise_cohesion(sources)
        if cohesion < OVER_MERGE_MIN_COHESION:
            issues.append(
                Issue(
                    "OVER_MERGE_LOW_COHESION",
                    LEVEL_REVIEW,
                    "合并簇内最弱一对的语义重合度仅 {:.3f}".format(cohesion),
                    entry.item_index,
                    {"sources": entry.source_indexes},
                )
            )

        if not entry.contiguous:
            issues.append(
                Issue(
                    "MERGED_EVIDENCE_NOT_CONTIGUOUS",
                    LEVEL_WARNING,
                    "来源 evidence 不连续，start/end 为包络区间",
                    entry.item_index,
                )
            )
    return issues


def _min_pairwise_cohesion(sources: List[SourceItem]) -> float:
    scores = []
    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            scores.append(
                max(
                    overlap_coefficient(sources[i].title, sources[j].title),
                    jaccard(sources[i].content, sources[j].content),
                )
            )
    return min(scores) if scores else 1.0


def check_under_merge(items: List[Dict[str, Any]]) -> List[Issue]:
    """归并后仍存在高度疑似同一事项的两条。只报，不自动再合。

    门槛定得高，而且要求部门与项目都一致：这个检查的价值在于"少而准"。
    实测阈值 0.62 且只比部门时，单份文档能报出 125 条，全是短标题的字面
    重合（"暂无。"、"持续推进"），把真正的漏合淹没掉，等于没有检查。
    """
    issues: List[Issue] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            left, right = items[i], items[j]
            if (left.get("department") or "") != (right.get("department") or ""):
                continue
            if (left.get("project") or "") != (right.get("project") or ""):
                continue
            # 内容太短的条目（"暂无。"）字面重合没有意义
            if min(len(str(left.get("content") or "")), len(str(right.get("content") or ""))) < 12:
                continue
            score = max(
                overlap_coefficient(
                    str(left.get("title") or ""), str(right.get("title") or "")
                ),
                jaccard(str(left.get("content") or ""), str(right.get("content") or "")),
            )
            if score >= UNDER_MERGE_SIMILARITY:
                issues.append(
                    Issue(
                        "UNDER_MERGE_SUSPECT",
                        LEVEL_WARNING,
                        "两条 Item 语义重合度 {:.3f}，疑似仍未归并".format(score),
                        i,
                        {"other_item_index": j, "titles": [left.get("title"), right.get("title")]},
                    )
                )
    return issues


def check_project_conflict(
    uncertain: List[Dict[str, Any]], alias_conflicts: List[Dict[str, Any]]
) -> List[Issue]:
    issues = [
        Issue(
            "PROJECT_ENTITY_UNCERTAIN",
            LEVEL_WARNING,
            "项目称谓拿不准，保持独立：{} / {}".format(row.get("left"), row.get("right")),
            None,
            {"reason": row.get("reason"), "project_names": [row.get("left"), row.get("right")]},
        )
        for row in uncertain
    ]
    issues.extend(
        Issue(
            "PROJECT_ALIAS_CONFLICT",
            LEVEL_REVIEW,
            "别名 {} 已指向另一个实体 {}".format(row.get("alias"), row.get("entity_id")),
            None,
            row,
        )
        for row in alias_conflicts
    )
    return issues


def check_generic_compatibility(
    items: List[Dict[str, Any]], source_mode: str
) -> List[Issue]:
    """generic 模式的兼容性：缺 work_section / project 不得判 ERROR。

    这里只在 block 模式下把大面积缺失报成提示，generic 模式一律不报，
    避免把 M1 的已知弱项当成 M2 的错误。
    """
    if source_mode == "generic":
        return []
    missing = sum(1 for item in items if not (item.get("work_section") or "").strip())
    if items and missing / len(items) > 0.9:
        return [
            Issue(
                "WORK_SECTION_MOSTLY_MISSING",
                LEVEL_WARNING,
                "block 模式下 {}/{} 条缺 work_section，检查上游结构切分".format(
                    missing, len(items)
                ),
            )
        ]
    return []


def run(
    items: List[Dict[str, Any]],
    trace: List[MergeTraceEntry],
    source_items: List[SourceItem],
    source_mode: str,
    schema_errors: List[str],
    uncertain_projects: List[Dict[str, Any]],
    alias_conflicts: List[Dict[str, Any]],
    alias_pool: set,
    extra_issues: Optional[List[Issue]] = None,
    project_mapping: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    by_index = {item.index: item for item in source_items}
    issues: List[Issue] = list(extra_issues or [])
    issues.extend(
        Issue("SCHEMA_VIOLATION", LEVEL_ERROR, message) for message in schema_errors
    )
    issues.extend(check_grounding(items, trace, by_index, alias_pool))
    issues.extend(check_over_merge(items, trace, by_index, source_mode, project_mapping))
    issues.extend(check_under_merge(items))
    issues.extend(check_project_conflict(uncertain_projects, alias_conflicts))
    issues.extend(check_generic_compatibility(items, source_mode))

    counts: Dict[str, int] = {}
    for issue in issues:
        counts[issue.code] = counts.get(issue.code, 0) + 1
    levels = {issue.level for issue in issues}
    if LEVEL_ERROR in levels:
        status = ERROR
    elif LEVEL_REVIEW in levels:
        status = REVIEW
    else:
        status = PASS

    return {
        "status": status,
        "issues": [issue.to_dict() for issue in issues],
        "issue_counts": counts,
        "_issue_objects": issues,
    }
