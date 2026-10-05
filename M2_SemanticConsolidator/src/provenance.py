# -*- coding: utf-8 -*-
"""追溯信息的组装与人工 REVIEW 样本导出。

要求：
* 每个输出 Item 都能回溯到它来自哪些 M1 Item（合并与未合并都写）；
* 所有来源 evidence 全部保留；
* 项目 alias 决策必须记录；
* **LLM 的 reasoning 原文不进正式输出**——只进 review 样本文件，
  那是给人看的，不是交给 M3/M6 的业务数据。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import re

from models import Issue, MergeTraceEntry, ProjectEntity
from text_utils import normalize_name


def build_project_cards(items, entities, trace):
    """一个明确父项目一张卡，子事项继续通过原 items/trace 独立处理。

    只接受同部门/交付组内已出现的完整父项目称谓和显式分隔符；
    不截断矿号、期次，不凭公共前缀猜父项目，不写永久别名。
    """
    entity_by_id = {e['entity_id']: e for e in entities}
    names_by_scope = {}
    rows = []
    for item, entry in zip(items, trace):
        scope = (item.get('department'), item.get('delivery_group'))
        names = entity_by_id.get(entry.project_entity_id, {}).get('source_names', [])
        names = set(names) | {item.get('project')}
        names.discard(None)
        names_by_scope.setdefault(scope, set()).update(names)
        rows.append((item, entry, scope, names))
    cards = {}
    for item, entry, scope, names in rows:
        parent_candidates = set()
        for name in names:
            # ponytail: explicit 项目-子项 hierarchy only; add source-heading IDs
            # if M1 later exposes hierarchy without an explicit project label.
            match = re.fullmatch(r'(.+项目)\s*[-—–:：]\s*(.+)', name)
            if match and match[1].strip() in names_by_scope[scope]:
                parent_candidates.add(match[1].strip())
        parent = next(iter(parent_candidates)) if len(parent_candidates) == 1 else None
        project = parent or item.get('project')
        key = (*scope, normalize_name(project)) if project else (*scope, None, entry.item_index)
        card = cards.setdefault(key, {
            'project': project, 'department': scope[0], 'delivery_group': scope[1],
            'item_indexes': [], 'source_indexes': [], 'source_projects': [],
        })
        card['item_indexes'].append(entry.item_index)
        card['source_indexes'].extend(entry.source_indexes)
        card['source_projects'] = sorted(set(card['source_projects']) | names)
    return list(cards.values())


def build_merge_trace(entries: List[MergeTraceEntry]) -> List[Dict[str, Any]]:
    return [entry.to_dict() for entry in entries]


def build_project_entities(
    entities: Dict[str, ProjectEntity], used_ids: Optional[set] = None
) -> List[Dict[str, Any]]:
    rows = []
    for entity_id in sorted(entities):
        if used_ids is not None and entity_id not in used_ids:
            continue
        rows.append(entities[entity_id].to_dict())
    return rows


def review_samples(
    items: List[Dict[str, Any]],
    trace: List[MergeTraceEntry],
    issues: List[Issue],
    source_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """把需要人工判断的条目整理成可直接看的样本。"""
    by_item: Dict[int, List[Issue]] = {}
    issues = [issue for issue in issues if issue.level in ('review', 'error')]
    for issue in issues:
        if issue.item_index is None:
            continue
        by_item.setdefault(issue.item_index, []).append(issue)

    samples = []
    for entry in trace:
        item_issues = by_item.get(entry.item_index, [])
        if not item_issues:
            continue
        samples.append(
            {
                "scope": "item",
                "item_index": entry.item_index,
                "merged": entry.merged,
                "issues": [issue.to_dict() for issue in item_issues],
                "merged_item": items[entry.item_index]
                if 0 <= entry.item_index < len(items)
                else None,
                "source_items": [
                    source_items[i] for i in entry.source_indexes if 0 <= i < len(source_items)
                ],
                "notes": entry.notes,
            }
        )

    # 文档级问题（项目实体 UNCERTAIN、MERGE_UNCERTAIN、整簇 REVIEW、结构冲突否决）
    # 没有 item_index，但它们恰恰是 REVIEW 的主要来源——实测全量 10 份里
    # PROJECT_ENTITY_UNCERTAIN 就有 33 条。漏掉它们等于人工没东西可看。
    for issue in issues:
        if issue.item_index is not None:
            continue
        sources = issue.detail.get("sources") if isinstance(issue.detail, dict) else None
        samples.append(
            {
                "scope": "document",
                "issues": [issue.to_dict()],
                "source_items": [
                    source_items[i]
                    for i in (sources or [])
                    if isinstance(i, int) and 0 <= i < len(source_items)
                ],
            }
        )
    return samples


def sft_records(
    pair_judgements: List[Any],
    entity_decisions: List[Dict[str, Any]],
    source_items: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """整理未来可用于 M2 SFT 的判定数据（需求第十八节 P2）。

    只保留确定性判定（SAME/DIFFERENT、MERGE/KEEP_SEPARATE），
    UNCERTAIN 不进训练集——那是待人工消解的，不是标签。
    """
    merge_rows = []
    for judgement in pair_judgements:
        if judgement.decision not in ("MERGE", "KEEP_SEPARATE"):
            continue
        if not (
            0 <= judgement.left < len(source_items)
            and 0 <= judgement.right < len(source_items)
        ):
            continue
        merge_rows.append(
            {
                "item_a": source_items[judgement.left],
                "item_b": source_items[judgement.right],
                "label": judgement.decision,
            }
        )
    entity_rows = [
        {
            "project_a": row.get("left") or row.get("name"),
            "project_b": row.get("right") or row.get("catalog_entity"),
            "label": row.get("decision"),
        }
        for row in entity_decisions
        if row.get("decision") in ("SAME_ENTITY", "DIFFERENT_ENTITY")
    ]
    return {"item_merge": merge_rows, "project_entity": entity_rows}
