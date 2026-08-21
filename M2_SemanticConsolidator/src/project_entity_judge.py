# -*- coding: utf-8 -*-
"""项目实体归一：候选召回 → LLM 判定 → alias 落库。

流程（对应需求第六节）::

    名称规范化 → 已有 alias 精确匹配 → 候选项目召回 → LLM Entity Judge
    → SAME_ENTITY / DIFFERENT_ENTITY / UNCERTAIN

只有 SAME_ENTITY 才写永久 alias；UNCERTAIN 进 REVIEW。
LLM 的 reason 只进 decisions 记录里的结构化字段，不把原始推理写进正式 items。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models import (
    DIFFERENT_ENTITY,
    ENTITY_DECISIONS,
    ENTITY_UNCERTAIN,
    SAME_ENTITY,
    ProjectEntity,
    SourceItem,
)
from project_candidate_retriever import (
    catalog_candidates,
    collect_project_names,
    pair_candidates,
)
from project_normalizer import ProjectCatalog
from text_utils import ambiguous_ordinal, normalize_name, ordinal_conflict

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "project_entity_judge.md"

# 一个项目名最多给模型看几条上下文 Item
CONTEXT_ITEMS = 3


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _context_for(name: str, items: List[SourceItem]) -> Dict[str, object]:
    """收集某个项目称谓在本次会议里的上下文，供模型判断归属。"""
    key = normalize_name(name)
    related = [item for item in items if normalize_name(item.project or "") == key]
    departments, groups, assignees = [], [], []
    for item in related:
        for value, bucket in (
            (item.get("department"), departments),
            (item.get("delivery_group"), groups),
        ):
            if isinstance(value, str) and value.strip() and value.strip() not in bucket:
                bucket.append(value.strip())
        for person in item.assignees:
            if person not in assignees:
                assignees.append(person)
    return {
        "project_name": name,
        "department": departments,
        "delivery_group": groups,
        "assignee": assignees,
        "items": [
            {"title": item.title, "content": item.content[:200]}
            for item in related[:CONTEXT_ITEMS]
        ],
    }


class UnionFind:
    def __init__(self, keys: List[str]):
        self.parent = {key: key for key in keys}

    def find(self, key: str) -> str:
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


class ProjectEntityJudge:
    def __init__(
        self,
        client=None,
        catalog: Optional[ProjectCatalog] = None,
        prompt: Optional[str] = None,
    ):
        self.client = client
        self.catalog = catalog or ProjectCatalog()
        self.prompt = prompt if prompt is not None else _load_prompt()
        self.calls = 0
        self.uncertain: List[Dict[str, object]] = []

    # ---- 单次判定 -------------------------------------------------
    def judge_pair(
        self, left_ctx: Dict[str, object], right_ctx: Dict[str, object]
    ) -> Tuple[str, Optional[str], str]:
        left_name = str(left_ctx["project_name"])
        right_name = str(right_ctx["project_name"])
        if normalize_name(left_name) == normalize_name(right_name):
            return SAME_ENTITY, left_name, "归一化后完全相同"
        # 确定性硬否决：序数/期次/年份不同一律不同实体，不问模型
        if ordinal_conflict(left_name, right_name):
            return DIFFERENT_ENTITY, None, "序数/期次/年份不同"
        if self.client is None:
            return ENTITY_UNCERTAIN, None, "未配置模型，无法判定"

        self.calls += 1
        payload = {"project_a": left_ctx, "project_b": right_ctx}
        result = self.client.complete_json(self.prompt, _dump(payload))
        if not isinstance(result, dict):
            return ENTITY_UNCERTAIN, None, "模型未返回合法 JSON"
        decision = str(result.get("decision") or "").strip().upper()
        if decision not in ENTITY_DECISIONS:
            return ENTITY_UNCERTAIN, None, "模型返回了未知 decision"
        reason = str(result.get("reason") or "")[:120]
        if decision != SAME_ENTITY:
            return decision, None, reason
        canonical = result.get("canonical_name")
        # canonical_name 必须原样是两个输入之一，禁止模型自造名字
        if not isinstance(canonical, str) or normalize_name(canonical) not in (
            normalize_name(left_name),
            normalize_name(right_name),
        ):
            return ENTITY_UNCERTAIN, None, "canonical_name 不在输入称谓中，拒绝采信"
        canonical = (
            left_name if normalize_name(canonical) == normalize_name(left_name) else right_name
        )
        return SAME_ENTITY, canonical, reason

    # ---- 整篇归一 -------------------------------------------------
    def resolve(self, items: List[SourceItem]) -> Tuple[Dict[str, ProjectEntity], Dict[str, str]]:
        """返回 (entity_id → ProjectEntity, 归一化名 → entity_id)。"""
        names = collect_project_names(item.raw for item in items)
        contexts = {name: _context_for(name, items) for name in names}
        union = UnionFind([normalize_name(n) for n in names])
        decisions: List[Dict[str, object]] = []
        canonical_vote: Dict[str, str] = {}
        # 本次会议里被判"不是同一个"的名字对。这个结论不许在后面
        # 通过项目主表绕过去——否则 UNCERTAIN 会被 catalog 悄悄合掉。
        blocked: set = set()

        # 1) 会议内部两两归一
        candidates = pair_candidates(names)
        ambiguous = self._ambiguous_names(names, candidates)
        for left, right, signals in candidates:
            if left in ambiguous or right in ambiguous:
                # 简称同时匹配多个序数不同的候选，归到任何一个都是赌博
                reason = "简称同时匹配到序数不同的多个候选，无法确定归属"
                decisions.append(
                    {
                        "type": "intra_meeting",
                        "left": left,
                        "right": right,
                        "decision": ENTITY_UNCERTAIN,
                        "recall_rule": signals.get("rule"),
                        "reason": reason,
                    }
                )
                self.uncertain.append({"left": left, "right": right, "reason": reason})
                blocked.add(frozenset((normalize_name(left), normalize_name(right))))
                continue
            decision, canonical, reason = self.judge_pair(contexts[left], contexts[right])
            decisions.append(
                {
                    "type": "intra_meeting",
                    "left": left,
                    "right": right,
                    "decision": decision,
                    "recall_rule": signals.get("rule"),
                    "reason": reason,
                }
            )
            if decision == SAME_ENTITY and canonical:
                union.union(normalize_name(left), normalize_name(right))
                canonical_vote[union.find(normalize_name(left))] = canonical
            else:
                blocked.add(frozenset((normalize_name(left), normalize_name(right))))
                if decision == ENTITY_UNCERTAIN:
                    self.uncertain.append({"left": left, "right": right, "reason": reason})

        # 2) 成组，选 canonical
        groups: Dict[str, List[str]] = {}
        for name in names:
            groups.setdefault(union.find(normalize_name(name)), []).append(name)

        entities: Dict[str, ProjectEntity] = {}
        name_to_entity: Dict[str, str] = {}
        for root, members in groups.items():
            canonical = canonical_vote.get(root)
            if not canonical or canonical not in members:
                # 组内没有模型给出的 canonical（例如单元素组）时，
                # 取信息量最大的原始写法，绝不拼接新名字。
                canonical = max(members, key=lambda value: (len(value), value))
            entity = self._link_catalog(
                canonical,
                members,
                contexts,
                decisions,
                blocked=blocked,
                skip_catalog=any(member in ambiguous for member in members),
            )
            entities[entity.entity_id] = entity
            for member in members:
                name_to_entity[normalize_name(member)] = entity.entity_id

        for entity in entities.values():
            entity.decisions = [
                d
                for d in decisions
                if d.get("left") in entity.members or d.get("right") in entity.members
                or d.get("name") in entity.members
            ]
        return entities, name_to_entity

    def _entity_is_blocked(self, entity_id: str, members: List[str], blocked: set) -> bool:
        """该主表实体已有的别名里，有没有与当前组"判过不是同一个"的名字。"""
        if not blocked:
            return False
        rows = self.catalog.conn.execute(
            "SELECT alias FROM project_aliases WHERE entity_id = ?", (entity_id,)
        ).fetchall()
        existing = {normalize_name(row["alias"]) for row in rows}
        for member in members:
            key = normalize_name(member)
            for other in existing:
                if frozenset((key, other)) in blocked:
                    return True
        return False

    @staticmethod
    def _ambiguous_names(names: List[str], candidates) -> set:
        """找出"无序数简称同时匹配多个序数不同候选"的名字。"""
        partners: Dict[str, List[str]] = {}
        for left, right, _signals in candidates:
            partners.setdefault(left, []).append(right)
            partners.setdefault(right, []).append(left)
        return {
            name
            for name, others in partners.items()
            if ambiguous_ordinal(name, others)
        }

    def _link_catalog(
        self,
        canonical: str,
        members: List[str],
        contexts: Dict[str, Dict[str, object]],
        decisions: List[Dict[str, object]],
        blocked: Optional[set] = None,
        skip_catalog: bool = False,
    ) -> ProjectEntity:
        """把本次会议的组挂到持久化的项目主表上。

        ``blocked`` 里的名字对是本次会议已经判过"不是同一个"的，
        不允许再通过项目主表把它们连到一起。
        """
        blocked = blocked or set()
        known = self.catalog.lookup(canonical)
        if known is None:
            for member in members:
                known = self.catalog.lookup(member)
                if known:
                    break
        if known is not None and self._entity_is_blocked(known["entity_id"], members, blocked):
            known = None
        if known is None and not skip_catalog:
            entries = self.catalog.all_projects()
            for candidate in catalog_candidates(canonical, entries):
                entry = candidate["entity"]
                if self._entity_is_blocked(entry["entity_id"], members, blocked):
                    continue
                decision, chosen, reason = self.judge_pair(
                    contexts.get(canonical, {"project_name": canonical}),
                    {"project_name": entry["canonical_name"], "from_catalog": True},
                )
                decisions.append(
                    {
                        "type": "catalog",
                        "name": canonical,
                        "catalog_entity": entry["entity_id"],
                        "decision": decision,
                        "reason": reason,
                    }
                )
                if decision == SAME_ENTITY:
                    known = {
                        "entity_id": entry["entity_id"],
                        "canonical_name": entry["canonical_name"],
                    }
                    break
                if decision == ENTITY_UNCERTAIN:
                    self.uncertain.append(
                        {"left": canonical, "right": entry["canonical_name"], "reason": reason}
                    )
        if known is None:
            known = self.catalog.ensure_project(canonical)
        # 只有确定同实体的写法才落成永久 alias
        for member in members:
            self.catalog.add_alias(str(known["entity_id"]), member, source="m2")
        return ProjectEntity(
            entity_id=str(known["entity_id"]),
            canonical_name=str(known["canonical_name"]),
            aliases=list(members),
            members=list(members),
        )


def _dump(payload: Dict[str, object]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)
