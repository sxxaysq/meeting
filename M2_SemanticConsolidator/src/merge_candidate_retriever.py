# -*- coding: utf-8 -*-
"""候选策略分发。

M2 只有一套 Common Core，两种模式只在**候选生成**这一层分叉，
Project Entity Resolver / Merge Judge / Cluster Validator / Quality Gate
全部共用。不维护两套 M2。

来源模式由 CLI 显式传入（``--source-mode block|generic``），
**不猜 JSON 内容**——猜错会让两种模式的约束互相污染。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import block_candidate_strategy
import generic_candidate_strategy
from models import MergeCandidate, SOURCE_MODES, SourceItem

STRATEGIES = {
    "block": block_candidate_strategy,
    "generic": generic_candidate_strategy,
}

# 单条 Item 最多进入几个候选对。候选爆炸会让 LLM 调用量失控，
# 也会显著抬高传递式错合的风险，所以按信号强度截断。
MAX_CANDIDATES_PER_ITEM = 6


def generate(
    source_mode: str,
    items: List[SourceItem],
    project_of: Dict[int, Optional[str]],
    max_gap: Optional[int] = None,
) -> List[MergeCandidate]:
    if source_mode not in STRATEGIES:
        raise ValueError(
            "未知 source_mode={!r}，只能是 {}".format(source_mode, "/".join(SOURCE_MODES))
        )
    module = STRATEGIES[source_mode]
    kwargs = {"max_gap": max_gap} if max_gap is not None else {}
    candidates = module.generate(items, project_of, **kwargs)
    return _cap(candidates)


def _strength(candidate: MergeCandidate) -> tuple:
    signals = candidate.signals
    return (
        float(signals.get("similarity") or 0.0) + (0.2 if signals.get("same_project_entity") else 0.0),
        -int(signals.get("gap") or 0),
    )


def _cap(candidates: List[MergeCandidate]) -> List[MergeCandidate]:
    """每条 Item 只保留信号最强的若干候选，保持判定量可控。"""
    ordered = sorted(candidates, key=_strength, reverse=True)
    counts: Dict[int, int] = {}
    kept: List[MergeCandidate] = []
    for candidate in ordered:
        if (
            counts.get(candidate.left, 0) >= MAX_CANDIDATES_PER_ITEM
            or counts.get(candidate.right, 0) >= MAX_CANDIDATES_PER_ITEM
        ):
            continue
        counts[candidate.left] = counts.get(candidate.left, 0) + 1
        counts[candidate.right] = counts.get(candidate.right, 0) + 1
        kept.append(candidate)
    kept.sort(key=lambda c: c.key)
    return kept
