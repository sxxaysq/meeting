"""入图后一致性校验（validator）。

针对单份文档验证：
1. MeetingItem 数量与 M2 items[] 一致；
2. 每个 MeetingItem 有且仅有一个 SourceDocument（HAS_ITEM）；
3. evidence_text 与 M2 原文逐字一致（原样保存，绝不改写）；
4. merge provenance（source_indexes/merged）与 M2 merge_trace 一致；
5. 非 PASS 强制入图的节点全部带 m3_ingest_mode=forced 标记。
"""
from __future__ import annotations

from M3_KnowledgeGraph.src import id_generator as ids
from M3_KnowledgeGraph.src.neo4j_store import Neo4jStore


def validate_document(store: Neo4jStore, envelope: dict) -> list[str]:
    m2 = envelope["m2_output"]
    doc_id = envelope["source_document_id"]
    issues: list[str] = []

    rows = store._run(
        """
        MATCH (d:SourceDocument)-[:HAS_ITEM]->(i:MeetingItem)
        WHERE $doc_id IN d.source_document_ids
          AND $doc_id IN i.source_document_ids
        RETURN i.id AS id, i.title AS title, i.evidence_text AS evidence_text,
               i.source_indexes AS source_indexes, i.merged AS merged,
               i.m3_ingest_mode AS ingest_mode,
               COUNT { (i)<-[:HAS_ITEM]-() } AS doc_links
        """,
        doc_id=doc_id,
    )
    graph_items = {r["id"]: r for r in rows}

    if len(rows) != len(m2["items"]):
        issues.append(f"MeetingItem 数量不一致：图内 {len(rows)} vs M2 {len(m2['items'])}")

    trace_by_index = {t["item_index"]: t for t in m2["merge_trace"]}
    for i, item in enumerate(m2["items"]):
        trace = trace_by_index[i]
        item_id = ids.meeting_item_id(doc_id, trace["source_indexes"])
        row = graph_items.get(item_id)
        if row is None:
            issues.append(f"items[{i}] 未入图: {item_id}")
            continue
        if row["doc_links"] != 1:
            issues.append(f"items[{i}] HAS_ITEM 入边数={row['doc_links']}（应为 1）")
        if row["evidence_text"] != item["evidence"]["text"]:
            issues.append(f"items[{i}] evidence_text 与 M2 原文不一致")
        if sorted(row["source_indexes"]) != sorted(trace["source_indexes"]):
            issues.append(f"items[{i}] source_indexes 不一致")
        if bool(row["merged"]) != bool(trace["merged"]):
            issues.append(f"items[{i}] merged 标记不一致")
        if envelope.get("ingest_mode") == "forced" and row["ingest_mode"] != "forced":
            issues.append(f"items[{i}] 缺少 forced 标记")

    # Project alias 绑定抽查：每个 project_entity 的 alias 节点都指向对应 canonical
    for pe in m2["project_entities"]:
        pid = ids.project_id(pe["canonical_name"])
        bad = store._run(
            """
            MATCH (a:ProjectAlias)-[:ALIAS_OF]->(p:Project {id: $pid})
            WHERE NOT a.name IN $names
            RETURN a.name AS name
            """,
            pid=pid,
            names=sorted(set(pe.get("aliases", []) + pe.get("source_names", []))),
        )
        for b in bad:
            issues.append(f"Project {pe['canonical_name']} 存在未登记 alias: {b['name']}")

    return issues
