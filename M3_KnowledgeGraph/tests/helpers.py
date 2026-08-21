"""测试辅助：图清空、内存 M2 dict 入图、节点/关系计数。"""
from __future__ import annotations

from M3_KnowledgeGraph.src import graph_mapper, m2_adapter
from M3_KnowledgeGraph.src.neo4j_store import Neo4jStore
from M3_KnowledgeGraph.src.ontology import NODE_LABELS


def wipe(store: Neo4jStore) -> None:
    store._run(
        "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) DETACH DELETE n",
        labels=list(NODE_LABELS),
    )


def ingest_dict(store: Neo4jStore, m2: dict, source_document_id: str,
                meeting_date: str, force: bool = False) -> tuple[dict, object]:
    """把一个内存中的 M2 dict 走完 adapter→mapper→store，返回 (envelope, graph)。"""
    ingest_mode = m2_adapter.check_quality_gate(m2, force=force)
    envelope = m2_adapter.build_envelope(m2, source_document_id=source_document_id,
                                         meeting_date=meeting_date)
    envelope["ingest_mode"] = ingest_mode
    graph = graph_mapper.map_envelope_to_graph(envelope, ingested_at="2026-01-01T00:00:00Z")
    store.ensure_schema()
    store.ingest(graph)
    return envelope, graph


def count_label(store: Neo4jStore, label: str) -> int:
    return store._run(f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"]


def count_rel(store: Neo4jStore, rel: str) -> int:
    return store._run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c")[0]["c"]
