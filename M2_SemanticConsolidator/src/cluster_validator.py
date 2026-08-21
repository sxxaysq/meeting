# -*- coding: utf-8 -*-
"""候选簇构建与整簇校验。

两两判定会产生传递式错合：A≈B、B≈C，但 A 与 C 其实是两回事。
所以先用并查集把 MERGE 对聚成簇，再把**整簇**重新交给模型判断
"它们是否真的构成一个业务事项"。

规模 >= 2 的簇里，size==2 已经被两两判定直接判过，默认不再复判
（``revalidate_pairs=True`` 可强制复判）；size>=3 一律复判。

``SPLIT_CLUSTER`` 时模型必须给出完整分组；分组不合法（漏项、重复、越界）
一律降级为整簇 REVIEW，不猜模型想说什么。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from merge_judge import item_view
from models import (
    CLUSTER_DECISIONS,
    CLUSTER_REVIEW,
    KEEP_CLUSTER,
    MERGE,
    SPLIT_CLUSTER,
    Cluster,
    PairJudgement,
    SourceItem,
)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "cluster_validator.md"

# 超过这个规模的簇几乎必然是传递式错合，即使模型说保留也强制进 REVIEW。
HARD_CLUSTER_LIMIT = 6


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_clusters(
    judgements: List[PairJudgement], items: List[SourceItem]
) -> List[Cluster]:
    """按 MERGE 判定做并查集聚簇；未参与合并的 Item 自成单元素簇。"""
    parent = {item.index: item.index for item in items}

    def find(key: int) -> int:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for judgement in judgements:
        if judgement.decision != MERGE:
            continue
        a, b = find(judgement.left), find(judgement.right)
        if a != b:
            parent[b] = a

    groups: Dict[int, List[int]] = {}
    for item in items:
        groups.setdefault(find(item.index), []).append(item.index)
    clusters = [Cluster(members=sorted(members)) for members in groups.values()]
    clusters.sort(key=lambda c: c.members[0])
    return clusters


class ClusterValidator:
    def __init__(
        self,
        client=None,
        prompt: Optional[str] = None,
        revalidate_pairs: bool = False,
    ):
        self.client = client
        self.prompt = prompt if prompt is not None else _load_prompt()
        self.revalidate_pairs = revalidate_pairs
        self.calls = 0
        self.counts = {KEEP_CLUSTER: 0, SPLIT_CLUSTER: 0, CLUSTER_REVIEW: 0}

    def validate(
        self,
        clusters: List[Cluster],
        items: List[SourceItem],
        canonical_project: Optional[Dict[int, Optional[str]]] = None,
    ) -> List[Cluster]:
        by_index = {item.index: item for item in items}
        canonical = canonical_project or {}
        result: List[Cluster] = []
        for cluster in clusters:
            if cluster.size == 1:
                result.append(cluster)
                continue
            if cluster.size == 2 and not self.revalidate_pairs:
                cluster.validated = True
                cluster.reason = "两两判定已直接判过，不重复校验"
                self.counts[KEEP_CLUSTER] += 1
                result.append(cluster)
                continue
            result.extend(self._validate_one(cluster, by_index, canonical))
        return result

    def _validate_one(
        self,
        cluster: Cluster,
        by_index: Dict[int, SourceItem],
        canonical: Dict[int, Optional[str]],
    ) -> List[Cluster]:
        if self.client is None:
            cluster.decision = CLUSTER_REVIEW
            cluster.reason = "未配置模型，无法校验整簇"
            self.counts[CLUSTER_REVIEW] += 1
            return [cluster]

        payload = {
            "items": [
                {
                    **item_view(by_index[index], canonical.get(index)),
                    "index": position,
                    "source_index": index,
                }
                for position, index in enumerate(cluster.members)
            ]
        }
        self.calls += 1
        response = self.client.complete_json(
            self.prompt, json.dumps(payload, ensure_ascii=False, indent=2)
        )
        decision = ""
        if isinstance(response, dict):
            decision = str(response.get("decision") or "").strip().upper()
        if decision not in CLUSTER_DECISIONS:
            decision = CLUSTER_REVIEW
        reason = ""
        if isinstance(response, dict):
            reason = str(response.get("reason") or "")[:160]

        if decision == KEEP_CLUSTER:
            if cluster.size > HARD_CLUSTER_LIMIT:
                # 规模硬上限：模型说保留也不信，转人工
                cluster.decision = CLUSTER_REVIEW
                cluster.reason = "簇规模 {} 超过硬上限 {}，转人工".format(
                    cluster.size, HARD_CLUSTER_LIMIT
                )
                cluster.validated = True
                self.counts[CLUSTER_REVIEW] += 1
                return [cluster]
            cluster.decision = KEEP_CLUSTER
            cluster.reason = reason
            cluster.validated = True
            self.counts[KEEP_CLUSTER] += 1
            return [cluster]

        if decision == SPLIT_CLUSTER:
            groups = _parse_groups(response, cluster.size)
            if groups is None:
                cluster.decision = CLUSTER_REVIEW
                cluster.reason = "模型给出的分组不合法，转人工"
                cluster.validated = True
                self.counts[CLUSTER_REVIEW] += 1
                return [cluster]
            self.counts[SPLIT_CLUSTER] += 1
            pieces = []
            for group in groups:
                members = sorted(cluster.members[position] for position in group)
                pieces.append(
                    Cluster(
                        members=members,
                        decision=KEEP_CLUSTER,
                        reason=reason,
                        validated=True,
                    )
                )
            pieces.sort(key=lambda c: c.members[0])
            return pieces

        cluster.decision = CLUSTER_REVIEW
        cluster.reason = reason or "模型判定 REVIEW"
        cluster.validated = True
        self.counts[CLUSTER_REVIEW] += 1
        return [cluster]


def _parse_groups(response: object, size: int) -> Optional[List[List[int]]]:
    """分组必须是输入下标的一个完整划分，否则视为不合法。"""
    if not isinstance(response, dict):
        return None
    raw = response.get("groups")
    if not isinstance(raw, list) or not raw:
        return None
    groups: List[List[int]] = []
    seen: List[int] = []
    for group in raw:
        if not isinstance(group, list) or not group:
            return None
        members = []
        for value in group:
            if not isinstance(value, int) or not 0 <= value < size:
                return None
            members.append(value)
            seen.append(value)
        groups.append(sorted(members))
    if sorted(seen) != list(range(size)):
        return None
    return groups
