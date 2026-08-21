"""M3_KnowledgeGraph 单元测试。

覆盖用户列出的 11 项测试重点 + forced 标记：
1. M2 PASS 正常导入（合成 + 真实文件）
2. M2 REVIEW 默认拒绝
3. M2 ERROR 默认拒绝
4. 重复导入幂等
5. 同 canonical（不同 entity_id）不产生重复 Project
6. alias 正确绑定 canonical Project
7. 同一 Person 节点复用
8. MeetingItem provenance 可追溯
9. evidence 原样保存
10. null work_section/delivery_group 可正常处理
11. block/generic 均可导入
12. forced 入图全节点带标记
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from M3_KnowledgeGraph.src import id_generator as ids
from M3_KnowledgeGraph.src import pipeline, validator
from M3_KnowledgeGraph.src.models import M2RejectedError
from M3_KnowledgeGraph.src.ontology import NODE_LABELS
from M3_KnowledgeGraph.tests import factories as F
from M3_KnowledgeGraph.tests.helpers import count_label, count_rel, ingest_dict

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PASS_FILE = REPO_ROOT / "M2_SemanticConsolidator/out_m2_generic/run0/2026-04-13.m2.json"


# 1. PASS 正常导入（合成样例）
def test_pass_normal_import(store):
    env, _ = ingest_dict(store, F.rich_pass_m2(), "docA.pdf", "2026-04-13")
    assert env["ingest_mode"] == "formal"
    assert count_label(store, "MeetingItem") == 3
    assert count_label(store, "Project") == 2
    assert count_label(store, "SourceDocument") == 1
    assert count_rel(store, "HAS_ITEM") == 3


# 1b. PASS 正常导入（真实 M2 文件 + 入图后一致性校验）
def test_real_pass_file_import_and_validate(store):
    envelope, _ = pipeline.load_and_gate(REAL_PASS_FILE, None, "2026.4.13.real.pdf")
    graph = pipeline.build_graph(envelope)
    store.ensure_schema()
    store.ingest(graph)
    issues = validator.validate_document(store, envelope)
    assert issues == []
    assert count_label(store, "MeetingItem") == len(envelope["m2_output"]["items"])


# 2. REVIEW 默认拒绝
def test_review_rejected_by_default(store):
    m2 = F.rich_pass_m2()
    m2["validation"]["status"] = "REVIEW"
    with pytest.raises(M2RejectedError):
        ingest_dict(store, m2, "docR.pdf", "2026-04-13")
    assert count_label(store, "MeetingItem") == 0


# 3. ERROR 默认拒绝
def test_error_rejected_by_default(store):
    m2 = F.rich_pass_m2()
    m2["validation"]["status"] = "ERROR"
    with pytest.raises(M2RejectedError):
        ingest_dict(store, m2, "docE.pdf", "2026-04-13")
    assert count_label(store, "MeetingItem") == 0


# 4. 重复导入幂等
def test_idempotent_reimport(store):
    for _ in range(2):
        ingest_dict(store, F.rich_pass_m2(), "docA.pdf", "2026-04-13")
    assert count_label(store, "MeetingItem") == 3
    assert count_label(store, "Project") == 2
    assert count_label(store, "Person") == 2
    assert count_label(store, "Department") == 1
    assert count_rel(store, "HAS_ITEM") == 3
    assert count_rel(store, "ABOUT_PROJECT") == 2


# 5. 同 canonical（不同 entity_id）不产生重复 Project
def test_same_canonical_no_dup_project(store):
    ingest_dict(store, F.rich_pass_m2(), "docA.pdf", "2026-04-13")
    ingest_dict(store, F.second_meeting_m2(), "docB.pdf", "2026-04-20")
    pid = ids.project_id("红沙泉二矿智能化建设项目")
    rows = store._run(
        "MATCH (p:Project {id: $pid}) RETURN p.m2_entity_ids AS eids, p.meeting_dates AS dates",
        pid=pid,
    )
    assert len(rows) == 1
    assert set(rows[0]["eids"]) == {"P0001", "P0009"}
    assert set(rows[0]["dates"]) == {"2026-04-13", "2026-04-20"}
    # 红沙泉 + 大柳塔，合计仍是 2，而不是 3
    assert count_label(store, "Project") == 2


# 6. alias 正确绑定 canonical Project（可用 alias 反查 canonical）
def test_alias_bound_to_canonical(store):
    ingest_dict(store, F.rich_pass_m2(), "docA.pdf", "2026-04-13")
    res = store.query_project("红沙泉二矿项目")  # 用 alias 查询
    assert res["matched"] is True
    assert res["project"]["canonical_name"] == "红沙泉二矿智能化建设项目"
    alias_names = {a["name"] for a in res["aliases"]}
    assert "红沙泉二矿项目" in alias_names
    assert count_rel(store, "ALIAS_OF") == 3


# 7. 同一 Person 节点复用
def test_person_reuse(store):
    ingest_dict(store, F.rich_pass_m2(), "docA.pdf", "2026-04-13")
    assert count_label(store, "Person") == 2  # 张三、李四
    rows = store._run(
        "MATCH (:Person {name: '张三'})<-[:ASSIGNED_TO]-(i:MeetingItem) RETURN count(i) AS c"
    )
    assert rows[0]["c"] == 2  # 张三出现在 item0 与 item1，仅一个节点两条入边


# 8. MeetingItem provenance 可追溯
def test_meeting_item_provenance(store):
    ingest_dict(store, F.rich_pass_m2(), "docA.pdf", "2026-04-13")
    item_id = ids.meeting_item_id("docA.pdf", [0, 1])
    rows = store.query_item_evidence(item_id)
    assert len(rows) == 1
    r = rows[0]
    assert r["merged"] is True
    assert sorted(r["source_indexes"]) == [0, 1]
    assert r["evidence_mode"] == "bounding_span"
    assert r["title_source"] == "llm"


# 9. evidence 原样保存（逐字 + 坐标）
def test_evidence_preserved_verbatim(store):
    m2 = F.rich_pass_m2()
    ingest_dict(store, m2, "docA.pdf", "2026-04-13")
    original = m2["items"][0]["evidence"]
    item_id = ids.meeting_item_id("docA.pdf", [0, 1])
    r = store.query_item_evidence(item_id)[0]
    assert r["evidence_text"] == original["text"]
    assert json.loads(r["evidence_json"]) == original


# 10. null work_section/delivery_group 可正常处理
def test_null_worksection_delivery_group(store):
    ingest_dict(store, F.rich_pass_m2(), "docA.pdf", "2026-04-13")
    # 只有 item0 有 work_section / delivery_group
    assert count_label(store, "WorkSection") == 1
    assert count_label(store, "DeliveryGroup") == 1
    assert count_rel(store, "UNDER_SECTION") == 1
    assert count_rel(store, "DELIVERED_BY") == 1
    # item2（全部 null）仍能入图，且没有组织/项目出边
    item2_id = ids.meeting_item_id("docA.pdf", [3])
    rows = store._run(
        "MATCH (i:MeetingItem {id: $id}) OPTIONAL MATCH (i)-[r]->() RETURN count(r) AS out_edges",
        id=item2_id,
    )
    assert rows[0]["out_edges"] == 0


# 11. block/generic 均可导入（source_mode 作为 provenance）
def test_block_and_generic_both_import(store):
    ingest_dict(store, F.rich_pass_m2(source_mode="generic"), "docG.pdf", "2026-04-13")
    ingest_dict(store, F.rich_pass_m2(source_mode="block"), "docB.pdf", "2026-04-14")
    assert count_label(store, "SourceDocument") == 2
    doc_modes = {r["m"] for r in store._run("MATCH (d:SourceDocument) RETURN d.source_mode AS m")}
    assert doc_modes == {"generic", "block"}
    item_modes = {r["m"] for r in store._run("MATCH (i:MeetingItem) RETURN DISTINCT i.source_mode AS m")}
    assert item_modes == {"generic", "block"}
    # 不同 doc id → MeetingItem 不共享；业务实体（Project/Person/Department）跨文档共享
    assert count_label(store, "MeetingItem") == 6
    assert count_label(store, "Project") == 2
    assert count_label(store, "Person") == 2
    assert count_label(store, "Department") == 1


# 12. forced 入图：全部业务节点带 m3_ingest_mode=forced 标记
def test_forced_marks_nodes(store):
    m2 = F.rich_pass_m2()
    m2["validation"]["status"] = "REVIEW"
    env, _ = ingest_dict(store, m2, "docF.pdf", "2026-04-13", force=True)
    assert env["ingest_mode"] == "forced"
    rows = store._run(
        "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) "
        "AND n.m3_ingest_mode <> 'forced' RETURN count(n) AS c",
        labels=list(NODE_LABELS),
    )
    assert rows[0]["c"] == 0
