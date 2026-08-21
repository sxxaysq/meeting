"""M3 入图流水线编排：adapter → mapper → store（→ 可选 graphiti episode）。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from M3_KnowledgeGraph.src import graph_mapper, m2_adapter
from M3_KnowledgeGraph.src.models import GraphInput
from M3_KnowledgeGraph.src.neo4j_store import Neo4jStore


def load_and_gate(
    m2_path: str | Path,
    meeting_date: str | None,
    source_document: str | None,
    force: bool = False,
) -> tuple[dict, str]:
    """读取 M2 输出 + 门禁 + 元数据解析，返回 (envelope, ingest_mode)。"""
    data = m2_adapter.load_m2_output(m2_path)
    ingest_mode = m2_adapter.check_quality_gate(data, force=force)

    date = meeting_date or m2_adapter.derive_meeting_date(m2_path)
    if not date:
        raise m2_adapter.M2AdapterError(
            "无法确定 meeting_date：文件名不含日期，请显式传 --meeting-date YYYY-MM-DD"
        )
    doc = source_document or Path(m2_path).stem
    envelope = m2_adapter.build_envelope(data, source_document_id=doc, meeting_date=date)
    envelope["ingest_mode"] = ingest_mode
    return envelope, ingest_mode


def build_graph(envelope: dict) -> GraphInput:
    ingested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return graph_mapper.map_envelope_to_graph(envelope, ingested_at=ingested_at)


def ingest_m2_file(
    store: Neo4jStore,
    m2_path: str | Path,
    meeting_date: str | None = None,
    source_document: str | None = None,
    force: bool = False,
    graphiti_episode: bool = False,
) -> dict:
    """一份 M2 输出入图。返回摘要 dict。"""
    envelope, ingest_mode = load_and_gate(m2_path, meeting_date, source_document, force=force)
    graph = build_graph(envelope)
    store.ensure_schema()
    write_stats = store.ingest(graph)

    result = {
        "m2_path": str(m2_path),
        "source_document_id": envelope["source_document_id"],
        "meeting_date": envelope["meeting_date"],
        "validation_status": envelope["m2_output"]["validation"]["status"],
        "ingest_mode": ingest_mode,
        "plan": graph.summary(),
        "write_stats": write_stats,
    }

    if graphiti_episode:
        from M3_KnowledgeGraph.src.graphiti_client import record_episode

        ep = record_episode(envelope)
        result["graphiti_episode"] = ep

    return result
