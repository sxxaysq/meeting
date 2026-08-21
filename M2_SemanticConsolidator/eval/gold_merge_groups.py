# -*- coding: utf-8 -*-
"""从人工标注推导 M2 的 gold 合并分组。

⚠️ 读这个文件前必须知道的一件事
--------------------------------
``items.annotation_v3.merge_group.json`` **文件里没有 merge_group 字段**，
合并信号隐含在 evidence 区间：一条 gold 的字符区间会覆盖多条 M1 Item。

把它和未合并版 ``items.annotation_v3.strict.json`` 对比，可以还原它的合并规则：

* 214 个多成员合并组，**214/214 在源顺序上完全连续**；
* 192/214 组内共享同一 ``(department, work_section)``；
* 但 **170/214 组内 ``project`` 不同**——例如把 ``CRM二期项目`` +
  ``数据中台项目`` + ``煤矿复合灾害监测预警系统项目`` 合成一条；
  ``红沙泉二矿项目`` 的 9 个子系统（数据中心 / 综合管控平台 / 火灾监测…）
  也被合成一条。

也就是说，这份标注的合并语义是**源段落粒度**，不是"同一业务目标"。
而 M2 需求明确规定后者不许合并（需求第七节原文举的就是红沙泉这个例子）。

**结论：不能拿它当 M2 merge 的唯一 gold。** 直接照它优化 merge recall，
会把 M2 训成 over-merge，而 over-merge 是需求里代价最高、无法逆转的错误。

进一步实测（逐组人工核对 54 个 project 同质的多成员组）：
**它们同样全部是"同段落里的不同业务目标"**，例如

* ``红沙泉二矿项目`` 一组 9 条 = 集控中心 / 数据中心 / 现场跟进 / 智能综合管控平台 /
  采场全景系统 / 数据管理平台 / 火灾监测招标 / 厂家调研 / 日常报表——
  需求第七节正是拿这个例子说"不能合"；
* 另一组 4 条 = 选煤厂论坛汇报 / 头盔式智能巡检场景梳理 / 评价系统研发整理 /
  科研项目月度例会——四件完全不同的事。

**结论：这份标注里没有一个"同一业务目标被 M1 拆开"的正例。**
因此 merge recall / precision 对着它算都没有业务含义，
把 recall 当优化目标只会把 M2 训成 over-merge。

（另外标注本身有重复：doc0 的 gold148/149/150 三条内容完全相同，
doc1 的 gold62/63 同样重复。计数时会重复计入。）

所以这里给出三个视图，评估脚本必须分别报告，且**都不是优化目标**：

``full``
    标注原样，段落粒度。**仅供参照**，用来说明 M2 与标注的粒度差多少。
``project_consistent``
    只保留组内 project 同质的 gold 组（214 组里 54 组）。**仅供参照**：
    实测它同样包含"同项目不同子系统"这类需求明令禁止的合并，
    所以它的 recall 偏低反而说明 M2 是对的。
``hard_negative``
    gold 侧 project 明确不同的 Item 对。合并这类在需求下**一定是错的**，
    与需求语义完全一致，是这份标注上**唯一可信的 merge 质量指标**。
    目标是 0 违例。

M2 真正的 merge 质量只能靠逐条人工核对实际发生的合并，
见 ``eval/dump_merges.py`` 与 README 里的案例章节。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# 预测 Item 有多少比例落在 gold 区间内才算归属该 gold
ASSIGN_MIN_RATIO = 0.5


def split_documents(items: List[dict]) -> List[List[dict]]:
    """标注是 10 份会议拼接的一个数组，start_char 回退处即文档边界。"""
    docs: List[List[dict]] = [[]]
    previous = -1
    for item in items:
        start = item["evidence"]["start_char"]
        if start < previous:
            docs.append([])
        docs[-1].append(item)
        previous = start
    return docs


def load_annotation(path: str) -> List[List[dict]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return split_documents(payload["items"])


def _span(item: dict) -> Tuple[int, int]:
    evidence = item["evidence"]
    return int(evidence["start_char"]), int(evidence["end_char"])


def _overlap(a: Tuple[int, int], b: Tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def assign_to_gold(
    predicted: List[dict], gold: List[dict], min_ratio: float = ASSIGN_MIN_RATIO
) -> Dict[int, int]:
    """预测下标 → gold 下标。取覆盖比例最大的 gold，低于阈值则不归属。"""
    assignment: Dict[int, int] = {}
    for pi, item in enumerate(predicted):
        span = _span(item)
        length = max(1, span[1] - span[0])
        best_index, best_ratio = None, 0.0
        for gi, gold_item in enumerate(gold):
            ratio = _overlap(span, _span(gold_item)) / length
            if ratio > best_ratio:
                best_index, best_ratio = gi, ratio
        if best_index is not None and best_ratio >= min_ratio:
            assignment[pi] = best_index
    return assignment


def project_homogeneous_gold(
    gold: List[dict], strict: List[dict]
) -> Set[int]:
    """哪些 gold 组的成员（在 strict 标注里）共享同一个 project。

    单成员组恒为同质。跨项目的合并组会被排除。
    """
    homogeneous: Set[int] = set()
    for gi, gold_item in enumerate(gold):
        gs, ge = _span(gold_item)
        members = [s for s in strict if gs <= _span(s)[0] and _span(s)[1] <= ge]
        projects = {
            (s.get("project") or "").strip() for s in members if (s.get("project") or "").strip()
        }
        if len(projects) <= 1:
            homogeneous.add(gi)
    return homogeneous


def gold_pairs(
    assignment: Dict[int, int], allowed_gold: Optional[Set[int]] = None
) -> Set[Tuple[int, int]]:
    """同属一个 gold 的预测下标两两组合 = 应该合并的正例对。"""
    groups: Dict[int, List[int]] = {}
    for pi, gi in assignment.items():
        if allowed_gold is not None and gi not in allowed_gold:
            continue
        groups.setdefault(gi, []).append(pi)
    pairs: Set[Tuple[int, int]] = set()
    for members in groups.values():
        members.sort()
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))
    return pairs


def hard_negative_pairs(
    predicted: List[dict], gold: List[dict], assignment: Dict[int, int]
) -> Set[Tuple[int, int]]:
    """gold 侧 project 明确不同的预测对——合并它们在需求下一定是错的。"""
    project_of: Dict[int, str] = {}
    for pi, gi in assignment.items():
        value = (gold[gi].get("project") or "").strip()
        if value:
            project_of[pi] = value
    indexes = sorted(project_of)
    pairs: Set[Tuple[int, int]] = set()
    for i in range(len(indexes)):
        for j in range(i + 1, len(indexes)):
            a, b = indexes[i], indexes[j]
            if project_of[a] != project_of[b]:
                pairs.add((a, b))
    return pairs


def merged_pairs_from_trace(merge_trace: List[dict]) -> Set[Tuple[int, int]]:
    """M2 实际合并掉的来源对。"""
    pairs: Set[Tuple[int, int]] = set()
    for entry in merge_trace:
        members = sorted(entry.get("source_indexes") or [])
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))
    return pairs
