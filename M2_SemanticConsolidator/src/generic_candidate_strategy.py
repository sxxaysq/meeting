# -*- coding: utf-8 -*-
"""generic 模式的候选生成。

generic 面向 ASR / OCR / 断句错误 / 编号丢失 / 排版混乱的文本，
结构上下文弱。M1 实测：``department`` / ``delivery_group`` 尚可，
``work_section`` **恒为 null**（``generic_extractor.normalize_task`` 硬编码），
``project`` 一致率从 block 的 0.803 掉到 0.527。

因此与 block 的区别：

* ``work_section`` 为 null **绝不**参与任何判断，更不能当作冲突；
* ``project`` 不一致**不直接**否决候选——先过 Project Entity Resolver，
  只有归一后仍是明确不同实体才否决；
* 更依赖 ``title + content + evidence + assignee`` 的整体语义；
* 项目实体归一比 block 更积极地参与召回（同实体直接放宽邻近性要求）。

自动合并仍然保守：这里只放宽**召回**，最终判定还是 LLM + 簇校验。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from models import MergeCandidate, SourceItem
from text_utils import jaccard, overlap_coefficient

# generic 的字符坐标同样来自 evidence_aligner，邻近性依然可用，
# 但断句错误会让同一事项被拆得更散，所以窗口比 block 宽。
MAX_GAP_CHARS = 800
FAR_GAP_CHARS = 2500
NEAR_SIMILARITY = 0.15
FAR_SIMILARITY = 0.40


def _conflict(left: Optional[str], right: Optional[str]) -> bool:
    a = (left or "").strip()
    b = (right or "").strip()
    return bool(a and b and a != b)


def _similarity(left: SourceItem, right: SourceItem) -> float:
    title = overlap_coefficient(left.title, right.title)
    body = jaccard(left.content, right.content)
    evidence = jaccard(
        str((left.get("evidence") or {}).get("text") or ""),
        str((right.get("evidence") or {}).get("text") or ""),
    )
    return max(title, body, evidence)


def _assignee_overlap(left: SourceItem, right: SourceItem) -> bool:
    a, b = set(left.assignees), set(right.assignees)
    return bool(a and b and a & b)


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
    candidates: List[MergeCandidate] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            left, right = items[i], items[j]
            # department 在 generic 下仍然可信，保留为硬约束
            if _conflict(left.get("department"), right.get("department")):
                continue
            # work_section 在 generic 下不可信，完全不参与判断（null 也不是错误）

            left_project = project_of.get(left.index)
            right_project = project_of.get(right.index)
            same_entity = bool(left_project and left_project == right_project)
            different_entity = bool(
                left_project and right_project and left_project != right_project
            )
            if different_entity:
                continue

            gap = _gap(left, right)
            score = _similarity(left, right)
            shared_person = _assignee_overlap(left, right)

            if gap > FAR_GAP_CHARS:
                # 距离很远时，只有同实体 + 高语义重合才召回
                if not (same_entity and score >= FAR_SIMILARITY):
                    continue
            elif gap > max_gap:
                if not (same_entity or shared_person or score >= FAR_SIMILARITY):
                    continue
            elif not (
                same_entity or shared_person or score >= NEAR_SIMILARITY
            ):
                continue

            candidates.append(
                MergeCandidate(
                    left=left.index,
                    right=right.index,
                    signals={
                        "strategy": "generic",
                        "gap": gap,
                        "similarity": round(score, 3),
                        "same_project_entity": same_entity,
                        "shared_assignee": shared_person,
                    },
                )
            )
    return candidates
