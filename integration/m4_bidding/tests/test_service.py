# -*- coding: utf-8 -*-
"""service.py HTTP 层测试（TestClient + 假 LLM + SQLite，无网络）。"""

import pytest
from conftest import FakeLLMClient, make_bidding_item, make_item

import service as service_module


class _PatchedClient(FakeLLMClient):
    """冒充 llm_client.LLMClient，供 monkeypatch 注入。"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    decisions = {0: (True, "TENDER", "编制招标文件")}

    def _fake_build_client(config=None):
        return _PatchedClient(decisions)

    monkeypatch.setattr(service_module, "LLMClient", _fake_build_client)
    dsn = "sqlite:///" + str(tmp_path / "m4.db")
    app = service_module.create_app(dsn=dsn)
    with TestClient(app) as test_client:
        yield test_client
    app.state.repository.close()


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["dialect"] == "sqlite"


def test_split_ok_and_persists(client, sqlite_repo):
    items = [make_bidding_item(), make_item()]
    resp = client.post(
        "/m4/split",
        json={
            "source_document_id": "2026-04-07",
            "file_name": "2026-04-07.items.json",
            "meeting_date": "2026-04-07",
            "items": items,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_count"] == 2
    assert body["bidding_count"] == 1
    assert body["filtered_count"] == 1
    # 招投标条目已带注记；filtered 保持九字段
    assert body["bidding"][0]["bidding_category"] == "TENDER"
    assert set(body["filtered"][0].keys()) == {
        "department", "work_section", "delivery_group", "project", "item_type",
        "assignee", "title", "content", "evidence",
    }
    # 已入库（幂等：重复调用行数不变）
    resp2 = client.post(
        "/m4/split",
        json={"source_document_id": "2026-04-07", "file_name": "2026-04-07.items.json",
              "items": items},
    )
    assert resp2.status_code == 200
    stats = client.get("/m4/stats", params={"source_document_id": "2026-04-07"}).json()
    assert stats["bidding_count"] == 1
    assert stats["by_category"] == {"TENDER": 1}


def test_split_rejects_invalid_schema(client):
    bad = make_item()
    bad.pop("evidence")  # 九字段缺一即拒
    resp = client.post("/m4/split", json={"source_document_id": "d1", "items": [bad]})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "m1_items schema validation failed"


def test_split_file_missing_returns_400(client):
    resp = client.post("/m4/split-file", json={"path": "/nonexistent/x.items.json"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# doc id 归一（HTTP 层）：向 ods_m1_source_documents 已登记的口径对齐
# --------------------------------------------------------------------------


def test_split_via_http_writes_under_resolved_m1_doc_id(client):
    """传规范日期 2026-08-24，落库必须落在 M1 登记的文件名形态 id 上。

    回归背景：修复前 M4 会照传进来的 id 原样写，于是 8.24 那份的
    origin_item_id = sha1("2026-08-24#idx")，JOIN 不回 M1 的
    sha1("2026.8.24信息公司周例会工作安排备忘录#idx")，血缘断掉。
    """
    from test_repository import FILENAME_FORM, _seed_m1_docs

    repo = client.app.state.repository
    _seed_m1_docs(repo, [(FILENAME_FORM, "2026-08-24")])

    resp = client.post("/m4/split", json={
        "source_document_id": "2026-08-24",
        "file_name": FILENAME_FORM + ".pdf",
        "meeting_date": None,
        "mode": "generic",
        "items": [make_bidding_item(), make_item()],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 响应里如实回报归一后的 id，不静默改写
    assert body["source_document_id"] == FILENAME_FORM

    assert repo.stats(FILENAME_FORM)["bidding_count"] == 1
    assert repo.stats("2026-08-24")["bidding_count"] == 0
    # meeting_date 没显式传，也要能从文件名形态里确定性兜底出来
    assert repo.stats(FILENAME_FORM)["document"]["meeting_date"] == "2026-08-24"


def test_stats_resolves_both_id_forms_to_same_result(client):
    """/m4/stats 传 2026-08-24 与传 M1 登记的文件名形态，必须查到同一份数据。"""
    from test_repository import FILENAME_FORM, _seed_m1_docs

    repo = client.app.state.repository
    _seed_m1_docs(repo, [(FILENAME_FORM, "2026-08-24")])
    client.post("/m4/split", json={
        "source_document_id": FILENAME_FORM,
        "file_name": FILENAME_FORM + ".pdf",
        "meeting_date": "2026-08-24",
        "mode": "generic",
        "items": [make_bidding_item(), make_item()],
    })

    by_date = client.get("/m4/stats", params={"source_document_id": "2026-08-24"}).json()
    by_name = client.get("/m4/stats", params={"source_document_id": FILENAME_FORM}).json()

    assert by_date["bidding_count"] == by_name["bidding_count"] == 1
    assert by_date["source_document_id"] == FILENAME_FORM
    # 发生了归一时，把调用方原始入参回显出来，便于排查
    assert by_date["requested_source_document_id"] == "2026-08-24"
    # 精确命中时不画蛇添足
    assert "requested_source_document_id" not in by_name
