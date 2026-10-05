# -*- coding: utf-8 -*-
"""runner.py 测试：全链路编排（no_llm，不联网）+ 错误语义 + 红线。"""

import json
import os
import threading
from pathlib import Path

import pytest
from conftest import guard_sys_path, make_item, query, scalar, seed_m1_document

guard_sys_path()
import repository as R  # noqa: E402
import runner  # noqa: E402


def run_doc(repo, doc_id, catalog_path, **kwargs):
    kwargs.setdefault("no_llm", True)
    return runner.consolidate_document(repo, doc_id, catalog_path=catalog_path, **kwargs)


# --------------------------------------------------------------------------
# 正常链路
# --------------------------------------------------------------------------


def test_consolidate_document_lands_rows_and_files(repo, seeded, catalog_path):
    result = run_doc(repo, "2026-04-07", catalog_path)
    assert result["status"] == "ok"
    assert result["skipped"] is False
    assert result["source_mode"] == "generic"
    assert result["m1_item_count"] == 3
    # no_llm 下所有判定都是 UNCERTAIN，保守原则要求一条都不合
    assert result["item_count"] == 3
    assert result["merge_operations"] == 0
    assert result["validation_status"] in ("PASS", "REVIEW")
    assert result["llm_calls"] == 0

    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_consolidated_documents") == 1
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 3
    # 质量门问题逐条落表，总数与 issue_counts 对得上
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_validation_issues") == sum(
        result["issue_counts"].values()
    )
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_project_entities") == result[
        "project_entity_count"
    ]


def test_authoritative_json_uses_m2_suffix(repo, seeded, catalog_path):
    """回归：doc id 形如 2026-04-07，用 Path.with_suffix 会把 ".m2" 当后缀吃掉。"""
    result = run_doc(repo, "2026-04-07", catalog_path)
    for key in ("m2_json", "report_json"):
        path = result[key]
        assert path, key
        assert os.path.exists(path), path
    assert result["m2_json"].endswith("2026-04-07.m2.json")
    assert result["report_json"].endswith("2026-04-07.m2.report.json")
    assert result["review_json"] is None  # 已拆开的不确定事项仅保留 warning。
    assert 'project_cards' in json.loads(Path(result['report_json']).read_text())

    payload = json.loads(Path(result["m2_json"]).read_text(encoding="utf-8"))
    # 权威源必须是完整的 M2 输出（五个顶层键），不是只存 items
    assert sorted(payload) == [
        "items", "merge_trace", "project_entities", "source_mode", "validation",
    ]
    assert len(payload["items"]) == 3
    assert len(payload["merge_trace"]) == 3


def test_authoritative_json_written_even_when_validation_is_review(repo, seeded, catalog_path):
    """REVIEW 是正常业务结果，必须落库；只有 ERROR / Schema 失败才拒绝。"""
    # 构造实际的旧别名冲突，而非依赖“不配置模型就审核”的旧行为。
    catalog = runner.ProjectCatalog(catalog_path)
    entity = catalog.ensure_project('红沙泉二矿项目')
    catalog.add_alias(entity['entity_id'], '红沙泉项目')
    catalog.close()
    seed_m1_document(repo, '2026-04-07', [
        make_item(project='红沙泉二矿项目'), make_item(project='红沙泉项目'),
        make_item(project='数据中台项目')])
    result = run_doc(repo, "2026-04-07", catalog_path)
    assert result["validation_status"] == "REVIEW"
    assert result['review_json'].endswith('2026-04-07.m2.review.json')
    assert Path(result['review_json']).exists()
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 3


def test_lineage_covers_every_m1_item_exactly_once(repo, seeded, catalog_path):
    run_doc(repo, "2026-04-07", catalog_path)
    run_doc(repo, "2026-04-13", catalog_path)
    assert scalar(repo, "SELECT COUNT(*) FROM dwd_m2_item_lineage") == 5
    assert scalar(
        repo,
        "SELECT COUNT(*) FROM dwd_m2_item_lineage l "
        "LEFT JOIN ods_m1_meeting_items m ON m.item_id = l.origin_item_id "
        "WHERE m.item_id IS NULL",
    ) == 0
    # 每条 M1 条目恰好被一个 M2 条目覆盖（不漏不重）
    assert scalar(repo, "SELECT COUNT(DISTINCT origin_item_id) FROM dwd_m2_item_lineage") == 5


def test_project_catalog_accumulates_across_documents(repo, seeded, catalog_path):
    """项目主表跨会议累积：同一份 catalog 跑两份文档，entity_id 不重复分配。"""
    run_doc(repo, "2026-04-07", catalog_path)
    first = {
        row["entity_id"]
        for row in query(
            repo, "SELECT entity_id FROM ods_m2_project_entities WHERE source_document_id=?",
            ("2026-04-07",),
        )
    }
    run_doc(repo, "2026-04-13", catalog_path)
    second = {
        row["entity_id"]
        for row in query(
            repo, "SELECT entity_id FROM ods_m2_project_entities WHERE source_document_id=?",
            ("2026-04-13",),
        )
    }
    assert first and second
    # 两份会议的项目互不相同，entity_id 必须各拿各的号，不能都从 P0001 重来
    assert not (first & second)


# --------------------------------------------------------------------------
# 增量与幂等
# --------------------------------------------------------------------------


def test_second_run_is_skipped_by_fingerprint(repo, seeded, catalog_path):
    first = run_doc(repo, "2026-04-07", catalog_path)
    assert first["status"] == "ok"
    second = run_doc(repo, "2026-04-07", catalog_path)
    assert second["status"] == "skipped"
    assert second["skipped"] is True
    assert second["reason"] == "input_fingerprint_unchanged"
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 3


def test_force_reruns_but_stays_idempotent(repo, seeded, catalog_path):
    run_doc(repo, "2026-04-07", catalog_path)
    before = {
        t: scalar(repo, "SELECT COUNT(*) FROM " + t)
        for t in (
            "ods_m2_consolidated_documents",
            "ods_m2_meeting_items",
            "ods_m2_project_entities",
            "ods_m2_validation_issues",
        )
    }
    result = run_doc(repo, "2026-04-07", catalog_path, force=True)
    assert result["status"] == "ok"
    assert result["skipped"] is False
    after = {
        t: scalar(repo, "SELECT COUNT(*) FROM " + t) for t in before
    }
    assert before == after


def test_consolidate_pending_only_touches_pending(repo, seeded, catalog_path):
    assert len(repo.list_pending()) == 2
    out = runner.consolidate_pending(repo, limit=1, no_llm=True, catalog_path=catalog_path)
    assert out["processed"] == 1
    assert out["pending_left"] == 1
    assert len(out["documents"]) == 1
    # 按会议时间序，先跑的必然是 0407
    assert out["documents"][0]["source_document_id"] == "2026-04-07"

    out = runner.consolidate_pending(repo, limit=5, no_llm=True, catalog_path=catalog_path)
    assert out["processed"] == 1
    assert out["pending_left"] == 0

    out = runner.consolidate_pending(repo, limit=5, no_llm=True, catalog_path=catalog_path)
    assert out["processed"] == 0
    assert out["documents"] == []
    assert out["status"] == "ok"


def test_consolidate_all_processes_every_document_in_order(repo, seeded, catalog_path):
    out = runner.consolidate_all(repo, no_llm=True, catalog_path=catalog_path)
    assert out["status"] == "ok"
    assert out["processed"] == 2
    assert out["failed"] == 0
    assert [d["source_document_id"] for d in out["documents"]] == ["2026-04-07", "2026-04-13"]
    assert out["totals"]["m1_item_count"] == 5
    assert out["totals"]["item_count"] == 5
    assert out["pending_left"] == 0


def test_consolidate_all_on_empty_platform_raises_404(repo, catalog_path):
    with pytest.raises(runner.M2Error) as exc:
        runner.consolidate_all(repo, no_llm=True, catalog_path=catalog_path)
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------
# 错误语义：不静默修正
# --------------------------------------------------------------------------


def test_unknown_document_raises_404(repo, catalog_path):
    with pytest.raises(runner.M2Error) as exc:
        run_doc(repo, "不存在的会议", catalog_path)
    assert exc.value.status_code == 404
    assert "不存在的会议" in exc.value.detail


def test_document_without_items_raises_400(repo, catalog_path):
    seed_m1_document(repo, "2026-05-01", [])
    with pytest.raises(runner.M2Error) as exc:
        run_doc(repo, "2026-05-01", catalog_path)
    assert exc.value.status_code == 400
    assert "没有条目" in exc.value.detail


def test_bad_source_mode_raises_400_and_does_not_guess(repo, catalog_path):
    """source_mode 必填且不从内容猜：mode 非法就拒绝，不退化成 generic。"""
    seed_m1_document(repo, "2026-05-02", [make_item()], mode="weird")
    with pytest.raises(runner.M2Error) as exc:
        run_doc(repo, "2026-05-02", catalog_path)
    assert exc.value.status_code == 400
    assert "不从内容猜" in exc.value.detail
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 0


def test_invalid_m1_input_raises_422_and_archives(repo, catalog_path):
    """九字段 Schema 不增不减：多一个字段就该被拒，且原始 payload 留档。"""
    bad = make_item(confidence=0.99)  # M1 新输出不该有这个字段
    seed_m1_document(repo, "2026-05-03", [bad])
    with pytest.raises(runner.M2Error) as exc:
        run_doc(repo, "2026-05-03", catalog_path)
    assert exc.value.status_code == 422
    assert exc.value.extra["errors"]
    archived = exc.value.extra["archived"]
    assert os.path.exists(archived)
    assert "m1_input_rejected" in os.path.basename(archived)
    assert json.loads(Path(archived).read_text(encoding="utf-8"))["items"][0]["confidence"] == 0.99
    # 拒绝入库：一条都不许落
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 0
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_consolidated_documents") == 0


def test_invalid_field_values_raise_422(repo, catalog_path):
    """title 空字符串违反 minLength:1，item_type 非法值违反四值枚举。

    不用 del item["title"] 造缺字段：那样连自测库的 NOT NULL 列都写不进去，
    测的是夹具而不是服务。这两种形态都能落库，但必被入口 Schema 拦下。
    """
    seed_m1_document(repo, "2026-05-04", [make_item(title="")])
    with pytest.raises(runner.M2Error) as exc:
        run_doc(repo, "2026-05-04", catalog_path)
    assert exc.value.status_code == 422
    assert any("title" in message for message in exc.value.extra["errors"])

    seed_m1_document(repo, "2026-05-05", [make_item(item_type="UNKNOWN_TYPE")])
    with pytest.raises(runner.M2Error) as exc:
        run_doc(repo, "2026-05-05", catalog_path)
    assert exc.value.status_code == 422
    assert any("item_type" in message for message in exc.value.extra["errors"])

    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 0


def test_batch_reports_failures_instead_of_swallowing(repo, catalog_path):
    """批量里单份失败不能静默吞掉：整体以非 2xx 上抛，并带出失败清单。"""
    seed_m1_document(repo, "2026-04-07", [make_item()])
    seed_m1_document(repo, "2026-04-08", [make_item(confidence=0.5)])  # 非法输入
    with pytest.raises(runner.M2Error) as exc:
        runner.consolidate_all(repo, no_llm=True, catalog_path=catalog_path)
    error = exc.value
    # 全是 422 类失败 → 整体 422（顿悟 HTTP 节点会 raise_for_status 标 failed）
    assert error.status_code == 422
    assert error.extra["failed"] == 1
    assert error.extra["processed"] == 1
    assert [f["source_document_id"] for f in error.extra["failures"]] == ["2026-04-08"]
    assert error.extra["failures"][0]["status_code"] == 422
    # 成功那份仍然落了库
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_consolidated_documents") == 1


def test_align_trace_rejects_uncovered_output_item():
    """merge_trace 必须覆盖每一条输出 Item；对不齐是上游契约被破坏，直接 500。"""
    trace = [{"item_index": 0, "source_indexes": [0], "merged": False}]
    with pytest.raises(runner.M2Error) as exc:
        runner._align_trace([{}, {}], trace)
    assert exc.value.status_code == 500
    assert "item_index=1" in exc.value.detail


def test_busy_lock_returns_409(repo, seeded, catalog_path):
    """归并串行化：抢不到锁直接 409，不排队（避免顿悟 HTTP 节点超时）。"""
    held = threading.Event()
    release = threading.Event()

    def hold():
        with runner._RUN_LOCK:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert held.wait(10)
        with pytest.raises(runner.M2Error) as exc:
            run_doc(repo, "2026-04-07", catalog_path)
        assert exc.value.status_code == 409
    finally:
        release.set()
        thread.join(10)


# --------------------------------------------------------------------------
# 红线：M1 侧数据一行不动
# --------------------------------------------------------------------------


def test_consolidation_does_not_modify_m1_tables(repo, seeded, catalog_path):
    before_docs = query(repo, "SELECT * FROM ods_m1_source_documents ORDER BY source_document_id")
    before_items = query(repo, "SELECT * FROM ods_m1_meeting_items ORDER BY item_id")

    runner.consolidate_all(repo, no_llm=True, catalog_path=catalog_path)

    assert query(repo, "SELECT * FROM ods_m1_source_documents ORDER BY source_document_id") == before_docs
    assert query(repo, "SELECT * FROM ods_m1_meeting_items ORDER BY item_id") == before_items
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 5


def test_runner_imports_m2_source_read_only():
    """runner 只把 M2 src 挂上 sys.path，绝不挂 M1 src（两边有同名模块）。"""
    source = (runner.HERE / "runner.py").read_text(encoding="utf-8")
    assert 'sys.path.insert(0, str(M2_SRC))' in source
    assert "M1_ROOT / \"src\"" not in source
    assert "M1_ROOT / 'src'" not in source
    # M1 侧只被用来取 Schema 文件路径
    assert str(runner.M1_ITEMS_SCHEMA).endswith("M1_Extraction/schemas/m1_items.schema.json")


def test_llm_backend_never_raises(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY", "不是 JSON")
    backend = runner.llm_backend()
    assert "error" in backend
