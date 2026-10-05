# -*- coding: utf-8 -*-
"""service.py HTTP 层测试（TestClient + SQLite + no_llm，无网络、不调模型）。

重点是顿悟智体平台 HTTP 节点缺陷的三条容错通道：
- body 为空（平台 Body 类型选 none）也能跑；
- body 是 JSON 字符串字面量（平台把字符串交给 httpx 的 json=）要能再解一层；
- header 里残留未解析的 {{占位符}} 要当没给，而不是当成 doc id。
"""

import json

import pytest
from conftest import guard_sys_path, make_item, scalar, seed_m1_document

guard_sys_path()


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    guard_sys_path()
    import service as service_module

    dsn = "sqlite:///" + str(tmp_path / "m2.db")
    app = service_module.create_app(dsn=dsn)
    with TestClient(app) as test_client:
        yield test_client
    app.state.repository.close()


@pytest.fixture()
def seeded_client(client):
    """两份会议共 5 条 M1 条目，模拟中台里已有的 M1 资产。

    项目主表走 conftest 里设的 M2_CATALOG_PATH（测试专用目录），
    与正式归并的 data/project_catalog.sqlite 完全隔离。
    """
    repo = client.app.state.repository
    seed_m1_document(
        repo,
        "2026-04-07",
        [make_item(), make_item(title="完成管控平台数据接入工作"), make_item(title="梳理数据标准")],
    )
    seed_m1_document(
        repo,
        "2026-04-13",
        [make_item(title="推进红沙泉二矿立项"), make_item(title="组织安全培训")],
    )
    return client


def _post(client, url, **kwargs):
    """统一带上 no_llm，测试不触网。"""
    sep = "&" if "?" in url else "?"
    return client.post(url + sep + "no_llm=true", **kwargs)


# --------------------------------------------------------------------------
# 只读端点
# --------------------------------------------------------------------------


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["dialect"] == "sqlite"
    assert body["dsn_scheme"] == "sqlite"
    assert body["default_workers"] >= 1
    assert "llm" in body and "catalog" in body and "m2_out_dir" in body


def test_pending_lists_m1_documents(seeded_client):
    body = seeded_client.get("/m2/pending").json()
    assert body["m1_document_count"] == 2
    assert body["pending_count"] == 2
    assert [row["source_document_id"] for row in body["pending"]] == ["2026-04-07", "2026-04-13"]
    assert all(row["reason"] == "never_consolidated" for row in body["pending"])

    limited = seeded_client.get("/m2/pending?limit=1").json()
    assert limited["pending_count"] == 1


def test_stats_before_and_after(seeded_client):
    before = seeded_client.get("/m2/stats?source_document_id=2026-04-07").json()
    assert before["document"] is None
    assert before["item_count"] == 0

    _post(seeded_client, "/m2/consolidate", json={"source_document_id": "2026-04-07"})
    after = seeded_client.get("/m2/stats?source_document_id=2026-04-07").json()
    assert after["document"]["validation_status"] in ("PASS", "REVIEW")
    assert after["item_count"] == 3
    assert after["source_item_count_covered"] == 3
    assert after["by_item_type"]


def test_stats_requires_parameter(seeded_client):
    assert seeded_client.get("/m2/stats").status_code == 422  # FastAPI 缺参


# --------------------------------------------------------------------------
# /m2/consolidate：doc id 的三条来源通道
# --------------------------------------------------------------------------


def test_consolidate_via_json_body(seeded_client):
    resp = _post(
        seeded_client, "/m2/consolidate", json={"source_document_id": "2026-04-07"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["m1_item_count"] == 3
    assert body["item_count"] == 3
    assert scalar(
        seeded_client.app.state.repository, "SELECT COUNT(*) FROM ods_m2_meeting_items"
    ) == 3


def test_consolidate_via_header_channel(seeded_client):
    """顿悟唯一能插值的通道是 headers，所以必须支持 X-Source-Document-Id。"""
    resp = _post(
        seeded_client,
        "/m2/consolidate",
        headers={"X-Source-Document-Id": "2026-04-13"},
    )
    assert resp.status_code == 200
    assert resp.json()["source_document_id"] == "2026-04-13"
    assert resp.json()["m1_item_count"] == 2


def test_consolidate_body_wins_over_header(seeded_client):
    resp = _post(
        seeded_client,
        "/m2/consolidate",
        json={"source_document_id": "2026-04-07"},
        headers={"X-Source-Document-Id": "2026-04-13"},
    )
    assert resp.status_code == 200
    assert resp.json()["source_document_id"] == "2026-04-07"


def test_consolidate_ignores_unresolved_placeholder(seeded_client):
    """平台解析不到变量时会把 {{...}} 原样发出，必须当没给，不能当成 doc id。"""
    resp = _post(
        seeded_client,
        "/m2/consolidate",
        headers={"X-Source-Document-Id": "{{node-1787743092871.source_document_id}}"},
    )
    assert resp.status_code == 400
    assert "source_document_id" in resp.json()["detail"]


def test_consolidate_without_any_doc_id_is_400(seeded_client):
    resp = _post(seeded_client, "/m2/consolidate")
    assert resp.status_code == 400
    assert "X-Source-Document-Id" in resp.json()["detail"]


def test_consolidate_accepts_stringified_json_body(seeded_client):
    """顿悟缺陷 2：字符串 body 被 httpx 的 json= 序列化成 JSON 字符串字面量。

    服务端收到的是 "\\"{...}\\"" 这种整体带引号的东西，必须再解一层。
    """
    inner = json.dumps({"source_document_id": "2026-04-07"}, ensure_ascii=False)
    resp = seeded_client.post(
        "/m2/consolidate?no_llm=true",
        content=json.dumps(inner),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["source_document_id"] == "2026-04-07"


def test_consolidate_rejects_double_stringified_body(seeded_client):
    """内层还是字符串（平台把占位符原样发出后又包了一层）→ 400，不猜。"""
    resp = seeded_client.post(
        "/m2/consolidate?no_llm=true",
        content=json.dumps(json.dumps("{{{不是 JSON}}}")),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_consolidate_rejects_malformed_body(seeded_client):
    resp = seeded_client.post(
        "/m2/consolidate?no_llm=true",
        content=b"{\xe4\xb8\x8d\xe9\x97\xad\xe5\x90\x88",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "不是合法 JSON" in resp.json()["detail"]


def test_consolidate_rejects_non_object_body(seeded_client):
    resp = seeded_client.post(
        "/m2/consolidate?no_llm=true",
        content=json.dumps([1, 2, 3]),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "JSON 对象" in resp.json()["detail"]


def test_consolidate_unknown_document_is_404(seeded_client):
    resp = _post(
        seeded_client, "/m2/consolidate", json={"source_document_id": "1999-01-01"}
    )
    assert resp.status_code == 404
    assert "1999-01-01" in resp.json()["detail"]


def test_consolidate_invalid_m1_input_is_422(client):
    repo = client.app.state.repository
    seed_m1_document(repo, "2026-05-03", [make_item(confidence=0.99)])
    resp = _post(client, "/m2/consolidate", json={"source_document_id": "2026-05-03"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["errors"]
    assert body["archived"]
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 0


def test_consolidate_bad_workers_is_400(seeded_client):
    resp = seeded_client.post(
        "/m2/consolidate?no_llm=true",
        content=json.dumps({"source_document_id": "2026-04-07", "workers": "很多"}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "workers" in resp.json()["detail"]


def test_consolidate_is_idempotent_over_http(seeded_client):
    repo = seeded_client.app.state.repository
    for _ in range(3):
        resp = _post(
            seeded_client,
            "/m2/consolidate",
            json={"source_document_id": "2026-04-07", "force": True},
        )
        assert resp.status_code == 200
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 3
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_consolidated_documents") == 1


# --------------------------------------------------------------------------
# /m2/run-pending：顿悟工作流走的就是这条
# --------------------------------------------------------------------------


def test_run_pending_with_empty_body(seeded_client):
    """平台 Body 类型选 none → 完全没有请求体，也必须能跑。"""
    resp = seeded_client.post("/m2/run-pending?limit=1&no_llm=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["processed"] == 1
    assert body["pending_left"] == 1
    assert body["documents"][0]["source_document_id"] == "2026-04-07"


def test_run_pending_drains_then_reports_zero(seeded_client):
    resp = seeded_client.post("/m2/run-pending?limit=10&no_llm=true")
    assert resp.json()["processed"] == 2
    assert resp.json()["pending_left"] == 0

    resp = seeded_client.post("/m2/run-pending?limit=10&no_llm=true")
    body = resp.json()
    assert resp.status_code == 200
    assert body["processed"] == 0
    assert body["documents"] == []


def test_run_pending_limit_defaults_to_one(seeded_client):
    resp = seeded_client.post("/m2/run-pending?no_llm=true")
    assert resp.json()["processed"] == 1


def test_run_pending_picks_up_changed_m1_input(seeded_client):
    repo = seeded_client.app.state.repository
    seeded_client.post("/m2/run-pending?limit=10&no_llm=true")
    assert repo.list_pending() == []

    # M1 侧重灌一份内容变了的 → 必须重新进入待办
    seed_m1_document(repo, "2026-04-07", [make_item(title="改过的标题")])
    resp = seeded_client.post("/m2/run-pending?limit=10&no_llm=true")
    body = resp.json()
    assert body["processed"] == 1
    assert body["reasons"]["2026-04-07"] == "m1_updated"
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items WHERE source_document_id='2026-04-07'") == 1


# --------------------------------------------------------------------------
# /m2/consolidate-all
# --------------------------------------------------------------------------


def test_consolidate_all_over_http(seeded_client):
    resp = seeded_client.post("/m2/consolidate-all?no_llm=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["processed"] == 2
    assert body["totals"]["m1_item_count"] == 5
    assert body["totals"]["item_count"] == 5

    # 第二次不带 force：全部按指纹跳过
    again = seeded_client.post("/m2/consolidate-all?no_llm=true").json()
    assert again["processed"] == 0
    assert again["skipped"] == 2


def test_consolidate_all_reports_batch_failure_as_non_2xx(client):
    repo = client.app.state.repository
    seed_m1_document(repo, "2026-04-07", [make_item()])
    seed_m1_document(repo, "2026-04-08", [make_item(confidence=0.5)])
    resp = client.post("/m2/consolidate-all?no_llm=true")
    # 顿悟 HTTP 节点会 raise_for_status，非 2xx 才能把节点标成 failed
    assert resp.status_code == 422
    body = resp.json()
    assert body["failed"] == 1
    assert body["processed"] == 1
    assert body["failures"][0]["source_document_id"] == "2026-04-08"
