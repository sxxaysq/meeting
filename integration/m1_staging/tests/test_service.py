# -*- coding: utf-8 -*-
"""§5.4 验收用例：422 校验失败 / upsert 幂等 / 字段一致性。"""

import copy
import hashlib

from conftest import make_body, make_item


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["dialect"] == "sqlite"


def test_ingest_ok_and_item_id_derivation(client):
    response = client.post("/m1/ingest", json=make_body())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["item_count"] == 1
    expected = "item:m1:" + hashlib.sha1(b"doc:test-001#0").hexdigest()
    assert data["item_ids"] == [expected]
    assert len(expected) == 48  # 与 MySQL CHAR(48) 对齐


def test_ingest_rejects_extra_field_422(client):
    """九字段不增：混入 confidence 必须 422，且不入库。"""
    bad_item = make_item()
    bad_item["confidence"] = 0.9
    response = client.post("/m1/ingest", json=make_body(items=[bad_item]))
    assert response.status_code == 422
    assert "confidence" in response.text or "Additional properties" in response.text
    stats = client.get("/m1/stats", params={"source_document_id": "doc:test-001"}).json()
    assert stats["item_count"] == 0


def test_ingest_rejects_bad_enum_422(client):
    bad_item = make_item(item_type="CREATE_TASK")
    response = client.post("/m1/ingest", json=make_body(items=[bad_item]))
    assert response.status_code == 422


def test_ingest_rejects_missing_required_422(client):
    bad_item = make_item()
    del bad_item["evidence"]
    response = client.post("/m1/ingest", json=make_body(items=[bad_item]))
    assert response.status_code == 422


def test_ingest_rejects_missing_evidence_key_422(client):
    bad_item = make_item()
    del bad_item["evidence"]["exact_match"]
    response = client.post("/m1/ingest", json=make_body(items=[bad_item]))
    assert response.status_code == 422


def test_ingest_idempotent_and_updated_at_refreshed(client):
    body = make_body(items=[make_item(), make_item(title="另一项任务", content="另一项内容")])
    first = client.post("/m1/ingest", json=body)
    assert first.status_code == 200
    stats1 = client.get("/m1/stats", params={"source_document_id": body["source_document_id"]}).json()
    assert stats1["item_count"] == 2

    second = client.post("/m1/ingest", json=body)
    assert second.status_code == 200
    stats2 = client.get("/m1/stats", params={"source_document_id": body["source_document_id"]}).json()
    assert stats2["item_count"] == 2, "重复 ingest 不允许产生重复行"
    assert stats2["document"]["updated_at"] >= stats1["document"]["updated_at"]
    assert stats2["exact_match_count"] == 2
    assert stats2["exact_match_rate"] == 1.0


def test_ingest_field_roundtrip(client):
    """入库后逐字段可还原出原始九字段 item。"""
    import json

    item = make_item()
    body = make_body(items=[item])
    assert client.post("/m1/ingest", json=body).status_code == 200
    repository = client.app.state.repository
    with repository._lock:
        row = repository._conn.execute(
            "SELECT * FROM ods_m1_meeting_items WHERE source_document_id = ?",
            (body["source_document_id"],),
        ).fetchone()
    restored = json.loads(row["item_json"])
    assert restored == item
    assert row["evidence_start_char"] == item["evidence"]["start_char"]
    assert row["exact_match"] == 1
    assert json.loads(row["assignees_json"]) == item["assignee"]


def test_ingest_file_endpoint(client, tmp_path):
    import json

    payload = {"items": [make_item()]}
    path = tmp_path / "2026-04-07.items.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    response = client.post("/m1/ingest-file", json={"path": str(path)})
    assert response.status_code == 200, response.text
    assert response.json()["source_document_id"] == "2026-04-07"
    stats = client.get("/m1/stats", params={"source_document_id": "2026-04-07"}).json()
    assert stats["item_count"] == 1


def test_ingest_file_missing_400(client):
    response = client.post("/m1/ingest-file", json={"path": "/nonexistent/x.items.json"})
    assert response.status_code == 400


def test_meeting_date_derived_from_doc_id(client):
    # 本语料文件名约定即会议日期：doc id 形如 YYYY-MM-DD 时确定性兜底
    assert client.post(
        "/m1/ingest", json=make_body(source_document_id="2026-04-07", meeting_date=None)
    ).status_code == 200
    repository = client.app.state.repository
    with repository._lock:
        row = repository._conn.execute(
            "SELECT meeting_date FROM ods_m1_source_documents WHERE source_document_id = ?",
            ("2026-04-07",),
        ).fetchone()
    assert row["meeting_date"] == "2026-04-07"

    # 非日期形态 doc id 不解析、不猜测，保持空
    assert client.post(
        "/m1/ingest",
        json=make_body(
            source_document_id="doc:test-002", file_name="other.items.json", meeting_date=None
        ),
    ).status_code == 200
    with repository._lock:
        row2 = repository._conn.execute(
            "SELECT meeting_date FROM ods_m1_source_documents WHERE source_document_id = ?",
            ("doc:test-002",),
        ).fetchone()
    assert row2["meeting_date"] is None


def test_reingest_with_fewer_items_clears_stale_tail(client):
    """重灌条目变少时必须清掉上一轮的尾行。

    回归背景（2026-09-04 数据集端到端测试实测踩到）：upsert 只按 item_id 更新，
    条目从 159 变 147 时 item_seq 147..158 的旧行留在表里，于是
    `ods_m1_source_documents.item_count`(147) 与 items 实际行数(159) 打架，
    下游 m2 的输入指纹、中台数据服务 API 全部跟着错。
    """
    three = [make_item(title="甲"), make_item(title="乙"), make_item(title="丙")]
    assert client.post("/m1/ingest", json=make_body(items=three)).status_code == 200
    stats = client.get("/m1/stats", params={"source_document_id": "doc:test-001"}).json()
    assert stats["item_count"] == 3
    assert stats["document"]["item_count"] == 3

    one = [make_item(title="甲乙丙合并成一条")]
    assert client.post("/m1/ingest", json=make_body(items=one)).status_code == 200
    stats = client.get("/m1/stats", params={"source_document_id": "doc:test-001"}).json()
    # 两个口径必须一致：实际行数 == 文档声称数
    assert stats["item_count"] == 1
    assert stats["document"]["item_count"] == 1


def test_reingest_with_more_items_keeps_growing(client):
    """反方向也要对：条目变多时正常追加，不能被清理逻辑误删。"""
    assert client.post("/m1/ingest", json=make_body(items=[make_item(title="甲")])).status_code == 200
    three = [make_item(title="甲"), make_item(title="乙"), make_item(title="丙")]
    assert client.post("/m1/ingest", json=make_body(items=three)).status_code == 200
    stats = client.get("/m1/stats", params={"source_document_id": "doc:test-001"}).json()
    assert stats["item_count"] == 3
    assert stats["document"]["item_count"] == 3
