# -*- coding: utf-8 -*-
"""M2 主链路。

    M1 items
        ↓ Project Entity Resolver      项目实体归一（block/generic 共用）
        ↓ Candidate Strategy           block / generic 分叉，只在这一层
        ↓ Item Merge Judge             两两语义判定
        ↓ Cluster Validation           整簇复判，切断传递式错合
        ↓ Merger                       字段生成，evidence 程序计算
        ↓ Quality Gate                 七类检查 → PASS / REVIEW / ERROR
        ↓ Schema Validator
    {"source_mode", "items", "project_entities", "merge_trace", "validation"}

保守原则贯穿全流程：只有 pair 判 MERGE **且** 整簇校验通过的才真正合并；
任何 UNCERTAIN 都保持拆开并进 REVIEW，不靠阈值把"拿不准"糊成"合"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import cluster_validator as cluster_validator_module
import merge_candidate_retriever
import merger
import provenance
import quality_gate
import schema_validator
from cluster_validator import ClusterValidator, build_clusters
from merge_judge import MergeJudge
from models import (
    CLUSTER_REVIEW,
    Cluster,
    ERROR,
    Issue,
    LEVEL_REVIEW,
    MERGE_UNCERTAIN,
    MergeTraceEntry,
    PairJudgement,
    ProjectEntity,
    SOURCE_MODES,
    SourceItem,
)
from project_entity_judge import ProjectEntityJudge
from project_normalizer import ProjectCatalog
from text_utils import normalize_name


def _structural_conflict(cluster: Cluster, items: List[SourceItem]) -> Optional[str]:
    """簇内 department / delivery_group 出现两个及以上互不相同的非空值。

    这类簇一定不该合：需求第十节要求结构字段冲突进 REVIEW，而"合了以后把
    字段置空"既丢信息又保留了错误合并，不如直接不合。
    """
    if cluster.size < 2:
        return None
    by_index = {item.index: item for item in items}
    for field in ("department", "delivery_group"):
        values = sorted(
            {
                str(by_index[i].get(field)).strip()
                for i in cluster.members
                if i in by_index
                and isinstance(by_index[i].get(field), str)
                and str(by_index[i].get(field)).strip()
            }
        )
        if len(values) > 1:
            return "{}={}".format(field, " / ".join(values[:4]))
    return None


@dataclass
class M2Result:
    source_mode: str
    items: List[Dict[str, Any]]
    project_entities: List[Dict[str, Any]]
    merge_trace: List[MergeTraceEntry]
    validation: Dict[str, Any]
    schema_errors: List[str]
    stats: Dict[str, Any] = field(default_factory=dict)
    review: List[Dict[str, Any]] = field(default_factory=list)
    sft: Dict[str, Any] = field(default_factory=dict)

    def payload(self) -> Dict[str, Any]:
        """正式业务输出。不含 LLM reasoning，不含诊断统计。"""
        return {
            "source_mode": self.source_mode,
            "items": self.items,
            "project_entities": self.project_entities,
            "merge_trace": provenance.build_merge_trace(self.merge_trace),
            "validation": {
                "status": self.validation["status"],
                "issues": self.validation["issues"],
                "issue_counts": self.validation.get("issue_counts", {}),
            },
        }

    def report(self) -> Dict[str, Any]:
        return {
            "source_mode": self.source_mode,
            "stats": self.stats,
            "validation_status": self.validation["status"],
            "issue_counts": self.validation.get("issue_counts", {}),
            "schema_errors": self.schema_errors,
        }


def run_pipeline(
    m1_payload: Dict[str, Any],
    source_mode: str,
    client=None,
    catalog: Optional[ProjectCatalog] = None,
    normalized_text: Optional[str] = None,
    workers: int = 1,
    max_gap: Optional[int] = None,
    generate_titles: bool = True,
    revalidate_pairs: bool = False,
) -> M2Result:
    if source_mode not in SOURCE_MODES:
        raise ValueError("source_mode 只能是 {}".format("/".join(SOURCE_MODES)))
    raw_items = m1_payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("输入不是 M1 的 {'items': [...]} 结构")

    items = [SourceItem(index=i, raw=raw) for i, raw in enumerate(raw_items)]
    catalog = catalog or ProjectCatalog()

    # ---- 1. 项目实体归一 -----------------------------------------
    entity_judge = ProjectEntityJudge(client=client, catalog=catalog)
    entities, name_to_entity = entity_judge.resolve(items)

    project_of: Dict[int, Optional[str]] = {}
    canonical_of: Dict[int, Optional[str]] = {}
    for item in items:
        entity_id = name_to_entity.get(normalize_name(item.project or "")) if item.project else None
        project_of[item.index] = entity_id
        canonical_of[item.index] = (
            entities[entity_id].canonical_name if entity_id else item.project
        )

    # ---- 2. 候选生成（唯一按模式分叉的一层）----------------------
    candidates = merge_candidate_retriever.generate(
        source_mode, items, project_of, max_gap=max_gap
    )

    # ---- 3. 两两语义判定 -----------------------------------------
    judge = MergeJudge(client=client, workers=workers)
    judgements = judge.judge(candidates, items, canonical_of)

    # ---- 4. 聚簇 + 整簇校验 --------------------------------------
    clusters = build_clusters(judgements, items)
    validator = ClusterValidator(client=client, revalidate_pairs=revalidate_pairs)
    clusters = validator.validate(clusters, items, canonical_of)

    # REVIEW 的簇不合并，拆回单条，但把 REVIEW 理由带出去
    extra_issues: List[Issue] = []
    final_clusters: List[Cluster] = []
    for cluster in clusters:
        conflict = _structural_conflict(cluster, items)
        if conflict:
            # 结构字段冲突是**合并否决**，不是"合并后把字段置空"。
            # 两两判定各自都不冲突（有一边为 null），但连成簇后就冲突了，
            # 这是传递式错合的一种；实测 05-25 有一簇 5 条跨了多个部门。
            for member in cluster.members:
                final_clusters.append(Cluster(members=[member], decision=CLUSTER_REVIEW))
            extra_issues.append(
                Issue(
                    "STRUCTURE_CONFLICT_VETO",
                    LEVEL_REVIEW,
                    "簇内结构字段冲突，已否决合并并保持拆开：{}".format(conflict),
                    None,
                    {"sources": cluster.members},
                )
            )
            continue
        if cluster.size > 1 and cluster.decision == CLUSTER_REVIEW:
            for member in cluster.members:
                final_clusters.append(Cluster(members=[member], decision=CLUSTER_REVIEW))
            extra_issues.append(
                Issue(
                    "CLUSTER_REVIEW",
                    LEVEL_REVIEW,
                    "整簇校验未通过，已保持拆开：{}".format(cluster.reason or "无理由"),
                    None,
                    {"sources": cluster.members},
                )
            )
        else:
            final_clusters.append(cluster)
    final_clusters.sort(key=lambda c: c.members[0])

    # UNCERTAIN 的 pair 也要显式进 REVIEW，不能悄悄当成 KEEP_SEPARATE
    for judgement in judgements:
        if judgement.decision == MERGE_UNCERTAIN:
            extra_issues.append(
                Issue(
                    "MERGE_UNCERTAIN",
                    LEVEL_REVIEW,
                    "两两判定为 UNCERTAIN，已保持拆开",
                    None,
                    {
                        "sources": [judgement.left, judgement.right],
                        "reason": judgement.reason,
                    },
                )
            )

    # ---- 5. 合并与字段生成 ---------------------------------------
    by_index = {item.index: item for item in items}
    out_items: List[Dict[str, Any]] = []
    trace: List[MergeTraceEntry] = []
    used_entities = set()
    title_client = client if generate_titles else None

    for position, cluster in enumerate(final_clusters):
        entity_ids = {project_of.get(i) for i in cluster.members if project_of.get(i)}
        entity_id = next(iter(entity_ids)) if len(entity_ids) == 1 else None
        canonical = entities[entity_id].canonical_name if entity_id else None
        if canonical is None:
            # 簇内项目实体不唯一时不强行归一，取首条来源的原始写法
            first = by_index[min(cluster.members)]
            canonical = canonical_of.get(first.index) or first.project
        if entity_id:
            used_entities.add(entity_id)

        item, entry, issues = merger.build_item(
            cluster=cluster,
            by_index=by_index,
            canonical_project=canonical,
            project_entity_id=entity_id,
            source_mode=source_mode,
            normalized_text=normalized_text,
            output_index=position,
            title_client=title_client,
        )
        out_items.append(item)
        trace.append(entry)
        extra_issues.extend(issues)

    # ---- 6. 质量门 -----------------------------------------------
    alias_pool = {
        normalize_name(alias)
        for entity in entities.values()
        for alias in [entity.canonical_name, *entity.aliases]
    }
    alias_conflicts: List[Dict[str, Any]] = []
    for entity in entities.values():
        for alias in entity.aliases:
            other = catalog.alias_conflict(alias, entity.entity_id)
            if other:
                alias_conflicts.append(
                    {"alias": alias, "entity_id": other, "claimed_by": entity.entity_id}
                )

    project_entities = provenance.build_project_entities(entities, used_entities)
    draft = {
        "source_mode": source_mode,
        "items": out_items,
        "project_entities": project_entities,
        "merge_trace": provenance.build_merge_trace(trace),
        "validation": {"status": "PASS", "issues": []},
    }
    schema_errors = schema_validator.validate(draft)

    validation = quality_gate.run(
        items=out_items,
        trace=trace,
        source_items=items,
        source_mode=source_mode,
        schema_errors=schema_errors,
        uncertain_projects=entity_judge.uncertain,
        alias_conflicts=alias_conflicts,
        alias_pool=alias_pool,
        extra_issues=extra_issues,
    )

    result = M2Result(
        source_mode=source_mode,
        items=out_items,
        project_entities=project_entities,
        merge_trace=trace,
        validation=validation,
        schema_errors=schema_errors,
        stats={
            "source_items": len(items),
            "output_items": len(out_items),
            "merged_clusters": sum(1 for c in final_clusters if c.size > 1),
            "merge_operations": len(items) - len(out_items),
            "candidates": len(candidates),
            "pair_decisions": judge.counts,
            "cluster_decisions": validator.counts,
            "project_entities": len(project_entities),
            "project_names": len(name_to_entity),
            "project_uncertain": len(entity_judge.uncertain),
            "llm_calls": {
                "entity_judge": entity_judge.calls,
                "merge_judge": judge.calls,
                "cluster_validator": validator.calls,
            },
            "llm": client.stats() if client is not None else None,
        },
        review=provenance.review_samples(
            out_items, trace, validation["_issue_objects"], raw_items
        ),
        sft=provenance.sft_records(
            judgements,
            [d for entity in entities.values() for d in entity.decisions],
            raw_items,
        ),
    )
    # _issue_objects 只在内部用，不进正式输出
    validation.pop("_issue_objects", None)
    return result
