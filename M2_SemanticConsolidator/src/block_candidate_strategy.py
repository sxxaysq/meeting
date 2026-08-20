# -*- coding: utf-8 -*-
"""block 模式的候选生成。

block 走的是确定性编号层级切分，``department`` / ``delivery_group`` /
``work_section`` 结构可靠（M1 实测 department 一致率 0.949、delivery_group 0.969），
所以这些字段可以当**较强约束**用来缩小候选。

硬否决（默认不进候选，也就不可能自动合并）：

* ``department`` 两边都非空且不同；
* ``delivery_group`` 两边都非空且不同；
* 归一后的项目实体明确不同；
* evidence 距离过远且没有语义连续性。

注意：项目相同**不是**合并理由，只是允许它们进入候选。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from models import MergeCandidate, SourceItem
from text_utils import jaccard, overlap_coefficient

# 相邻条目之间允许的最大字符间隔。M1 的坐标系里一行 = 一个最内层编号条目，
# gold 里被拆开的同一事项全部是源顺序连续的，所以邻近性是很强的召回信号。
MAX_GAP_CHARS = 400
# 距离拉远时要求更高的语义重合度
FAR_GAP_CHARS = 1200
NEAR_SIMILARITY = 0.18
FAR_SIMILARITY = 0.45


def _conflict(left: Optional[str], right: Optional[str]) -> bool:
    """两边都非空且不同 → 明确冲突。有一边为空不算冲突。"""
    a = (left or "").strip()
    b = (right or "").strip()
    return bool(a and b and a != b)


def _similarity(left: SourceItem, right: SourceItem) -> float:
    """标题与正文的字符级重合度，只用于召回。"""
    title = overlap_coefficient(left.title, right.title)
    body = jaccard(left.content, right.content)
    return max(title, body)


def effective_projects(
    items: List[SourceItem], project_of: Dict[int, Optional[str]]
) -> Dict[int, Optional[str]]:
    """给没有 project 的 Item 补上它所处的项目段落上下文。

    block 模式下原文是"项目名 → 若干条目"的层级结构，M1 会把项目名写进
    有项目归属的条目，但像"跟进商机推进情况"这种短句可能留空。
    这时它实际归属的是**同部门内最近一个带项目的条目**所在的段落。

    实测价值：05-11 的 `跟进…电子采购平台商机问题结症反馈进度` 与
    `跟进商机推进情况` 两条 project 都是 null、部门相同、位置相近，
    没有这个上下文就会被错合，而标注里它们分属两个不同项目。
    """
    ordered = sorted(items, key=lambda item: item.span)
    effective: Dict[int, Optional[str]] = {}
    last_by_department: Dict[str, Optional[str]] = {}
    for item in ordered:
        department = (item.get("department") or "").strip()
        own = project_of.get(item.index)
        if own:
            last_by_department[department] = own
            effective[item.index] = own
        else:
            effective[item.index] = last_by_department.get(department)
    return effective


def _gap(left: SourceItem, right: SourceItem) -> int:
    ls, le = left.span
    rs, re_ = right.span
    if le <= rs:
        return rs - le
    if re_ <= ls:
        return ls - re_
    return 0


def generate(
    items: List[SourceItem],
    project_of: Dict[int, Optional[str]],
    max_gap: int = MAX_GAP_CHARS,
) -> List[MergeCandidate]:
    """``project_of`` 是 index → 归一后的 entity_id（无项目时 None）。"""
    candidates: List[MergeCandidate] = []
    effective = effective_projects(items, project_of)
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            left, right = items[i], items[j]
            if _conflict(left.get("department"), right.get("department")):
                continue
            if _conflict(left.get("delivery_group"), right.get("delivery_group")):
                continue
            left_project = project_of.get(left.index)
            right_project = project_of.get(right.index)
            if left_project and right_project and left_project != right_project:
                continue
            # 必须处在同一个项目段落。注意这里 None 也是一个有意义的段落
            # （"该部门第一个项目名出现之前"），不能当通配符：
            # 05-11 的 `跟进…电子采购平台商机…` 在项目名出现之前，
            # `跟进商机推进情况` 在 `高质量数据集数据标注建设项目` 之后，
            # 两条 project 都是 null，只有段落上下文能把它们分开。
            if effective.get(left.index) != effective.get(right.index):
                continue

            gap = _gap(left, right)
            if gap > FAR_GAP_CHARS:
                continue
            score = _similarity(left, right)
            same_section = not _conflict(
                left.get("work_section"), right.get("work_section")
            )
            if gap <= max_gap:
                # 邻近：同项目或同板块即可召回，语义判定交给 LLM
                recalled = (
                    (left_project is not None and left_project == right_project)
                    or same_section
                    or score >= NEAR_SIMILARITY
                )
                if not recalled:
                    continue
            elif score < FAR_SIMILARITY:
                continue

            candidates.append(
                MergeCandidate(
                    left=left.index,
                    right=right.index,
                    signals={
                        "strategy": "block",
                        "gap": gap,
                        "similarity": round(score, 3),
                        "same_project_entity": bool(
                            left_project and left_project == right_project
                        ),
                        "same_work_section": same_section,
                    },
                )
            )
    return candidates
