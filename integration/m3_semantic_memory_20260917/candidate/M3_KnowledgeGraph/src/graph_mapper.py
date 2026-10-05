"""M2 → 图谱的确定性映射（M3 的核心）。

输入：通过门禁的 M2 输出信封（m2_output + source_document_id + meeting_date）
输出：GraphInput（节点/边写入计划）

铁律：
- M2 JSON 是事实源：不重新抽实体、不重新合并、不重新归一、不改 evidence；
- 无 LLM 参与；同一输入必然得到同一计划（纯函数）；
- block/generic 不做两套逻辑，source_mode 只作为 provenance。
"""
from __future__ import annotations

import json

from M3_KnowledgeGraph.src import id_generator as ids
from M3_KnowledgeGraph.src.models import GraphInput, M2AdapterError
from M3_KnowledgeGraph.src.ontology import INGEST_MODE_FORCED


def map_envelope_to_graph(envelope: dict, ingested_at: str) -> GraphInput:
    m2 = envelope["m2_output"]
    doc_id = envelope["source_document_id"]
    meeting_date = envelope["meeting_date"]

    items = m2["items"]
    project_entities = m2["project_entities"]
    merge_trace = m2["merge_trace"]
    source_mode = m2["source_mode"]
    validation_status = m2["validation"]["status"]
    # forced 标记在 adapter 层决定，这里从信封透传
    ingest_mode = envelope.get("ingest_mode") or ("forced" if validation_status != "PASS" else "formal")

    graph = GraphInput(
        source_document_id=doc_id,
        meeting_date=meeting_date,
        source_mode=source_mode,
        validation_status=validation_status,
        ingested_at=ingested_at,
        ingest_mode=ingest_mode,
    )

    # ---------- SourceDocument ----------
    doc_node_id = ids.source_document_id(doc_id)
    graph.add_node(
        "SourceDocument",
        doc_node_id,
        {
            "name": doc_id,
            "meeting_date": meeting_date,
            "source_mode": source_mode,
            "validation_status": validation_status,
            "item_count": len(items),
        },
    )

    # ---------- Project（权威来源：project_entities[]） ----------
    entity_index: dict[str, dict] = {}
    for pe in project_entities:
        entity_index[pe["entity_id"]] = pe
        pid = ids.project_id(pe["canonical_name"])
        graph.add_node(
            "Project",
            pid,
            {
                "canonical_name": pe["canonical_name"],
                "aliases": sorted(set(pe.get("aliases", []))),
                "source_names": sorted(set(pe.get("source_names", []))),
                "m2_entity_ids": [pe["entity_id"]],
                "m2_decision_count": len(pe.get("decisions", [])),
            },
        )
        # alias 独立成节点，便于追踪来源
        for alias in sorted(set(pe.get("aliases", []) + pe.get("source_names", []))):
            aid = ids.alias_id(pid, alias)
            graph.add_node(
                "ProjectAlias",
                aid,
                {
                    "name": alias,
                    "canonical_name": pe["canonical_name"],
                    "kind": "alias" if alias in pe.get("aliases", []) else "source_name",
                },
            )
            graph.add_edge("ALIAS_OF", "ProjectAlias", aid, "Project", pid)

    # ---------- merge_trace 索引 ----------
    trace_by_index: dict[int, dict] = {}
    for t in merge_trace:
        if t["item_index"] in trace_by_index:
            raise M2AdapterError(f"merge_trace 出现重复 item_index={t['item_index']}")
        trace_by_index[t["item_index"]] = t

    # ---------- items[] → MeetingItem 及组织/人员节点 ----------
    seen_nodes: set[tuple[str, str]] = set()

    def ensure_node(label: str, node_id: str, props: dict) -> None:
        key = (label, node_id)
        if key in seen_nodes:
            return
        seen_nodes.add(key)
        graph.add_node(label, node_id, props)

    for i, item in enumerate(items):
        trace = trace_by_index.get(i)
        if trace is None:
            # M2 契约保证每条 item 有 trace；缺失视为结构异常
            raise M2AdapterError(f"items[{i}] 缺少对应 merge_trace")
        item_id = ids.meeting_item_id(doc_id, trace["source_indexes"])

        evidence = item["evidence"]
        graph.add_node(
            "MeetingItem",
            item_id,
            {
                "item_index": i,
                "item_type": item["item_type"],
                "title": item["title"],
                "content": item["content"],
                # evidence 原样保存：全文 JSON（含字符坐标）+ 纯文本两份，绝不改写
                "evidence_text": evidence["text"],
                "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                "source_mode": source_mode,
                # provenance（merge_trace）
                "source_indexes": sorted(trace["source_indexes"]),
                "merged": trace["merged"],
                "evidence_mode": trace["evidence_mode"],
                "evidence_contiguous": trace["evidence_contiguous"],
                "title_source": trace.get("title_source", "source"),
                "m2_project_entity_id": trace.get("project_entity_id") or "",
            },
        )
        graph.add_edge("HAS_ITEM", "SourceDocument", doc_node_id, "MeetingItem", item_id)

        # Department
        if item.get("department"):
            dep_id = ids.department_id(item["department"])
            ensure_node("Department", dep_id, {"name": ids.normalize_name(item["department"])})
            graph.add_edge("BELONGS_TO", "MeetingItem", item_id, "Department", dep_id)

        # WorkSection（允许 department 缺失：identity 中 department 记为空串）
        if item.get("work_section"):
            sec_id = ids.work_section_id(item.get("department"), item["work_section"])
            ensure_node(
                "WorkSection",
                sec_id,
                {
                    "name": ids.normalize_name(item["work_section"]),
                    "department_name": ids.normalize_name(item.get("department") or "") or None,
                },
            )
            graph.add_edge("UNDER_SECTION", "MeetingItem", item_id, "WorkSection", sec_id)
            if item.get("department"):
                graph.add_edge(
                    "UNDER_DEPARTMENT", "WorkSection", sec_id, "Department",
                    ids.department_id(item["department"]),
                )

        # DeliveryGroup（identity = name + department）
        if item.get("delivery_group"):
            dg_id = ids.delivery_group_id(item.get("department"), item["delivery_group"])
            ensure_node(
                "DeliveryGroup",
                dg_id,
                {
                    "name": ids.normalize_name(item["delivery_group"]),
                    "department_name": ids.normalize_name(item.get("department") or "") or None,
                },
            )
            graph.add_edge("DELIVERED_BY", "MeetingItem", item_id, "DeliveryGroup", dg_id)
            if item.get("department"):
                graph.add_edge(
                    "IN_DEPARTMENT", "DeliveryGroup", dg_id, "Department",
                    ids.department_id(item["department"]),
                )
            if item.get("work_section"):
                graph.add_edge(
                    "IN_SECTION", "DeliveryGroup", dg_id, "WorkSection",
                    ids.work_section_id(item.get("department"), item["work_section"]),
                )

        # Project（M2 已判定归属，直接用 entity_id 指向 canonical）
        if item.get("project"):
            entity_id = trace.get("project_entity_id")
            pe = entity_index.get(entity_id) if entity_id else None
            if pe is not None:
                pid = ids.project_id(pe["canonical_name"])
            else:
                # 有项目名但没有 M2 实体判定：按原始名称落节点，等后续会议 alias 补齐
                pid = ids.project_id(item["project"])
                ensure_node(
                    "Project",
                    pid,
                    {
                        "canonical_name": ids.normalize_name(item["project"]),
                        "aliases": [],
                        "source_names": [ids.normalize_name(item["project"])],
                        "m2_entity_ids": [],
                        "m2_decision_count": 0,
                        "resolved_by_m2": False,
                    },
                )
            graph.add_edge("ABOUT_PROJECT", "MeetingItem", item_id, "Project", pid)

        # Person
        for name in item.get("assignee", []):
            p_id = ids.person_id(name)
            ensure_node("Person", p_id, {"name": ids.normalize_name(name)})
            graph.add_edge("ASSIGNED_TO", "MeetingItem", item_id, "Person", p_id)

    # 去重边（同一对节点同类型关系只保留一条）
    seen_edges: set[tuple] = set()
    deduped = []
    for e in graph.edges:
        key = (e.rel_type, e.src_label, e.src_id, e.dst_label, e.dst_id)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        deduped.append(e)
    graph.edges = deduped

    if ingest_mode == INGEST_MODE_FORCED:
        for n in graph.nodes:
            n.props["m3_ingest_mode"] = "forced"

    return graph
