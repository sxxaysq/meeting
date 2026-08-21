# -*- coding: utf-8 -*-
"""Item 语义归并的两两判定。

召回信号（相似度、邻近性、同项目）**不出现在给模型的输入里**——
它们只是这对 Item 被送到模型面前的原因，写进提示词只会诱导模型往合并方向靠。
模型看到的就是两条 Item 的业务内容本身。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from models import (
    KEEP_SEPARATE,
    MERGE,
    MERGE_DECISIONS,
    MERGE_UNCERTAIN,
    MergeCandidate,
    PairJudgement,
    SourceItem,
)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "item_merge_judge.md"

CONTENT_LIMIT = 600


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def item_view(item: SourceItem, canonical_project: Optional[str] = None) -> Dict[str, object]:
    """给模型看的 Item 视图。只给业务内容，不给召回信号。"""
    return {
        "index": item.index,
        "department": item.get("department"),
        "delivery_group": item.get("delivery_group"),
        "project": canonical_project if canonical_project is not None else item.project,
        "item_type": item.get("item_type"),
        "assignee": item.assignees,
        "title": item.title,
        "content": item.content[:CONTENT_LIMIT],
    }


class MergeJudge:
    def __init__(self, client=None, prompt: Optional[str] = None, workers: int = 1):
        self.client = client
        self.prompt = prompt if prompt is not None else _load_prompt()
        self.workers = max(1, workers)
        self.calls = 0
        self.counts = {MERGE: 0, KEEP_SEPARATE: 0, MERGE_UNCERTAIN: 0}

    def _judge_one(
        self,
        candidate: MergeCandidate,
        by_index: Dict[int, SourceItem],
        canonical: Dict[int, Optional[str]],
    ) -> PairJudgement:
        left = by_index[candidate.left]
        right = by_index[candidate.right]
        if self.client is None:
            return PairJudgement(
                candidate.left, candidate.right, MERGE_UNCERTAIN, "未配置模型"
            )
        payload = {
            "item_a": item_view(left, canonical.get(left.index)),
            "item_b": item_view(right, canonical.get(right.index)),
        }
        self.calls += 1
        result = self.client.complete_json(
            self.prompt, json.dumps(payload, ensure_ascii=False, indent=2)
        )
        if not isinstance(result, dict):
            return PairJudgement(
                candidate.left, candidate.right, MERGE_UNCERTAIN, "模型未返回合法 JSON"
            )
        decision = str(result.get("decision") or "").strip().upper()
        if decision not in MERGE_DECISIONS:
            decision = MERGE_UNCERTAIN
        return PairJudgement(
            candidate.left, candidate.right, decision, str(result.get("reason") or "")[:120]
        )

    def judge(
        self,
        candidates: List[MergeCandidate],
        items: List[SourceItem],
        canonical_project: Optional[Dict[int, Optional[str]]] = None,
    ) -> List[PairJudgement]:
        by_index = {item.index: item for item in items}
        canonical = canonical_project or {}
        if not candidates:
            return []
        if self.workers == 1:
            results = [self._judge_one(c, by_index, canonical) for c in candidates]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(
                    pool.map(lambda c: self._judge_one(c, by_index, canonical), candidates)
                )
        for judgement in results:
            self.counts[judgement.decision] = self.counts.get(judgement.decision, 0) + 1
        return results
