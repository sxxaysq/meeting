# -*- coding: utf-8 -*-
"""repository.py 测试：ID 派生 / 指纹 / DDL 解析 / 幂等 / 血缘 / 只读红线。"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from conftest import (
    INTEGRATION_DIR,
    M2_SERVICE_DIR,
    guard_sys_path,
    make_item,
    query,
    scalar,
    seed_m1_document,
)

guard_sys_path()
import repository as R  # noqa: E402


def make_doc_row(source_document_id, items, **overrides):
    """一行合法的 ods_m2_consolidated_documents 记录。

    report_json 缺省跟随最终的 item_count（而不是输入条目数），
    避免覆盖 item_count 时两个字段互相矛盾。
    """
    row = {
        "source_document_id": source_document_id,
        "file_name": source_document_id + ".items.json",
        "meeting_date": source_document_id,
        "source_mode": "generic",
        "m1_item_count": len(items),
        "item_count": len(items),
        "merge_operations": 0,
        "merged_clusters": 0,
        "project_entity_count": 1,
        "validation_status": "REVIEW",
        "issue_count": 1,
        "input_fingerprint": R.fingerprint_items(items),
        "llm_calls": 7,
        "elapsed_ms": 1234,
    }
    row.update(overrides)
    row.setdefault(
        "report_json",
        json.dumps({"stats": {"output_items": row["item_count"]}}, ensure_ascii=False),
    )
    return row


def make_trace(count, merged_at=(), entity_id="P0001"):
    """与 count 条输出对齐的 merge_trace；merged_at 里的下标标记为合并产物（2 条来源）。"""
    trace = []
    cursor = 0
    for idx in range(count):
        merged = idx in merged_at
        sources = [cursor, cursor + 1] if merged else [cursor]
        cursor += len(sources)
        trace.append(
            {
                "item_index": idx,
                "source_indexes": sources,
                "merged": merged,
                "evidence_mode": "primary_source" if merged else "single",
                "evidence_contiguous": True,
                "source_evidence": [],
                "project_entity_id": entity_id,
                "title_source": "llm" if merged else "source",
                "notes": [],
            }
        )
    return trace


ENTITIES = [
    {
        "entity_id": "P0001",
        "canonical_name": "智能综合管控平台",
        "aliases": ["智能综合管控平台", "管控平台"],
        "source_names": ["智能综合管控平台"],
        "decisions": [{"type": "intra_meeting", "decision": "SAME_ENTITY"}],
    }
]
ISSUES = [
    {"code": "MERGE_UNCERTAIN", "level": "review", "message": "两两判定为 UNCERTAIN",
     "detail": {"sources": [0, 1]}},
    {"code": "PROJECT_ENTITY_UNCERTAIN", "level": "review", "message": "项目实体拿不准"},
]


# --------------------------------------------------------------------------
# ID 派生：血缘能不能 JOIN 回 M1，全看这里
# --------------------------------------------------------------------------


def _load_m1_staging_repository():
    """按文件路径加载 m1_staging/repository.py。

    不能用 import repository：两个目录下的模块同名，谁在 sys.path 前面就解析到谁。
    """
    path = INTEGRATION_DIR / "m1_staging" / "repository.py"
    spec = importlib.util.spec_from_file_location("m1_staging_repository", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_origin_item_id_matches_m1_staging_byte_for_byte():
    """回链 ID 必须与 m1_staging 的 derive_item_id 逐字节一致，否则血缘视图全空。"""
    m1_repo = _load_m1_staging_repository()
    for doc in ("2026-04-07", "2026.8.24信息公司周例会工作安排备忘录"):
        for idx in (0, 1, 137, 2076):
            assert R.derive_origin_item_id(doc, idx) == m1_repo.derive_item_id(doc, idx)


def test_item_id_shape():
    item_id = R.derive_item_id("2026-04-07", 3)
    assert item_id.startswith("item:m2:")
    assert len(item_id) == 48  # CHAR(48) 列宽
    assert re.fullmatch(r"item:m2:[0-9a-f]{40}", item_id)
    # 稳定：同 doc 同下标必同 ID；换下标必换 ID
    assert item_id == R.derive_item_id("2026-04-07", 3)
    assert item_id != R.derive_item_id("2026-04-07", 4)
    assert item_id != R.derive_origin_item_id("2026-04-07", 3)


def test_meeting_date_only_parsed_when_shaped_like_a_date():
    assert R.meeting_date_from_doc_id("2026-04-07") == "2026-04-07"
    # 非日期形态不猜测（本语料里确实有中文文件名的 doc id）
    assert R.meeting_date_from_doc_id("2026.8.24信息公司周例会工作安排备忘录") is None


# --------------------------------------------------------------------------
# 输入指纹
# --------------------------------------------------------------------------


def test_fingerprint_ignores_key_order_and_formatting():
    """MySQL JSON 列与 SQLite TEXT 列的序列化不同，指纹必须只看内容。"""
    a = [{"title": "x", "project": "y"}, {"title": "z", "project": None}]
    b = [{"project": "y", "title": "x"}, {"project": None, "title": "z"}]
    assert R.fingerprint_items(a) == R.fingerprint_items(b)
    # 内容真变了才必须变
    c = [{"title": "x", "project": "y"}, {"title": "z2", "project": None}]
    assert R.fingerprint_items(a) != R.fingerprint_items(c)
    assert re.fullmatch(r"[0-9a-f]{40}", R.fingerprint_items(a))


# --------------------------------------------------------------------------
# DDL 解析（bootstrap-schema 的唯一来源）
# --------------------------------------------------------------------------


def test_load_mysql_ddl_is_clean_and_idempotent():
    statements = R.load_mysql_ddl()
    assert len(statements) == 9  # 4 表 + 5 视图
    for stmt in statements:
        head = stmt.splitlines()[0]
        assert head.startswith("CREATE TABLE IF NOT EXISTS ods_m2_") or head.startswith(
            "CREATE OR REPLACE VIEW "
        ), head
        # 小节标题行的残留文字不能被并进第一条语句
        assert "§" not in stmt
        assert not stmt.strip().endswith(",")
    tables = [s for s in statements if s.startswith("CREATE TABLE")]
    assert len(tables) == 4
    assert {re.search(r"ods_m2_\w+", s).group(0) for s in tables} == {
        "ods_m2_consolidated_documents",
        "ods_m2_meeting_items",
        "ods_m2_project_entities",
        "ods_m2_validation_issues",
    }


def test_bootstrap_schema_reports_tables_and_views(repo):
    result = repo.bootstrap_schema()
    assert result["dialect"] == "sqlite"
    assert result["tables"] == 6  # 4 张 ods_m2_* + 2 张只读的 ods_m1_* 自测源表
    assert result["views"] == 5


# --------------------------------------------------------------------------
# 读侧
# --------------------------------------------------------------------------


def test_fetch_document_roundtrips_nine_fields(repo):
    items = [make_item(), make_item(title="另一条")]
    seed_m1_document(repo, "2026-04-07", items)
    doc = repo.fetch_document("2026-04-07")
    assert doc["source_document_id"] == "2026-04-07"
    assert doc["mode"] == "generic"
    assert doc["meeting_date"] == "2026-04-07"
    assert doc["item_count"] == 2
    assert len(doc["items"]) == 2
    # item_json 必须无损还原九字段，一条不多一条不少
    assert sorted(doc["items"][0]) == [
        "assignee", "content", "delivery_group", "department", "evidence",
        "item_type", "project", "title", "work_section",
    ]
    assert doc["items"][1]["title"] == "另一条"


def test_fetch_document_missing_returns_none(repo):
    assert repo.fetch_document("不存在的会议") is None
    assert repo.fetch_items("不存在的会议") == []


def test_list_documents_ordered_by_doc_id(repo, seeded):
    rows = repo.list_documents()
    assert [r["source_document_id"] for r in rows] == sorted(seeded)


def test_list_pending_lifecycle(repo):
    items = [make_item()]
    seed_m1_document(repo, "2026-04-07", items)

    pending = repo.list_pending()
    assert len(pending) == 1
    assert pending[0]["reason"] == "never_consolidated"
    assert pending[0]["m1_item_count"] == 1

    repo.upsert_consolidation(make_doc_row("2026-04-07", items), items, make_trace(1), ENTITIES, ISSUES)
    assert repo.list_pending() == []

    # M1 侧内容变了 → 必须重新出现在待办里，且理由不同
    seed_m1_document(repo, "2026-04-07", [make_item(title="改过的标题")])
    pending = repo.list_pending()
    assert len(pending) == 1
    assert pending[0]["reason"] == "m1_updated"


def test_list_pending_respects_limit(repo, seeded):
    assert len(repo.list_pending(limit=1)) == 1
    assert len(repo.list_pending(limit=2)) == 2
    assert len(repo.list_pending()) == 2


def test_document_without_items_is_not_pending(repo):
    """M1 侧登记了文档但没有条目：不是 M2 能处理的输入，跳过而不是报错。"""
    seed_m1_document(repo, "2026-05-01", [])
    assert repo.list_pending() == []


# --------------------------------------------------------------------------
# 写侧：幂等 / stale 清理 / 子表替换
# --------------------------------------------------------------------------


def test_upsert_is_idempotent(repo):
    items = [make_item(), make_item(title="第二条")]
    seed_m1_document(repo, "2026-04-07", items)
    doc_row = make_doc_row("2026-04-07", items)

    first = repo.upsert_consolidation(doc_row, items, make_trace(2), ENTITIES, ISSUES)
    assert first == {
        "source_document_id": "2026-04-07",
        "item_count": 2,
        "project_entity_count": 1,
        "issue_count": 2,
    }
    counts = _table_counts(repo)

    for _ in range(3):
        repo.upsert_consolidation(doc_row, items, make_trace(2), ENTITIES, ISSUES)
    assert _table_counts(repo) == counts
    assert counts["ods_m2_consolidated_documents"] == 1
    assert counts["ods_m2_meeting_items"] == 2
    assert counts["ods_m2_project_entities"] == 1
    assert counts["ods_m2_validation_issues"] == 2


def test_upsert_clears_stale_tail_rows(repo):
    """重跑后输出条目变少时，不能残留上一轮的尾行。"""
    items = [make_item(title="甲"), make_item(title="乙"), make_item(title="丙")]
    seed_m1_document(repo, "2026-04-07", items)
    repo.upsert_consolidation(
        make_doc_row("2026-04-07", items, item_count=3), items, make_trace(3), ENTITIES, ISSUES
    )
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_meeting_items") == 3

    fewer = [make_item(title="甲乙丙合并")]
    repo.upsert_consolidation(
        make_doc_row("2026-04-07", items, item_count=1, merge_operations=2),
        fewer,
        make_trace(1, merged_at=(0,)),
        ENTITIES,
        ISSUES,
    )
    rows = query(repo, "SELECT item_seq, title, merged, source_count FROM ods_m2_meeting_items")
    assert len(rows) == 1
    assert rows[0]["item_seq"] == 0
    assert rows[0]["merged"] == 1
    assert rows[0]["source_count"] == 2


def test_child_tables_are_replaced_not_accumulated(repo):
    items = [make_item()]
    seed_m1_document(repo, "2026-04-07", items)
    doc_row = make_doc_row("2026-04-07", items)

    repo.upsert_consolidation(doc_row, items, make_trace(1), ENTITIES, ISSUES)
    # 第二轮问题数变了：必须是替换，不是累加
    repo.upsert_consolidation(doc_row, items, make_trace(1), ENTITIES, ISSUES[:1])
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_validation_issues") == 1
    repo.upsert_consolidation(doc_row, items, make_trace(1), [], [])
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_project_entities") == 0
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m2_validation_issues") == 0


def test_row_mapping_carries_m2_annotations(repo):
    items = [make_item(), make_item(title="第二条"), make_item(title="第三条")]
    seed_m1_document(repo, "2026-04-07", items)
    # 3 条 M1 → 2 条 M2：第 0 条是 0+1 合并来的，第 1 条是原样保留的第 2 条
    trace = make_trace(2, merged_at=(0,))
    repo.upsert_consolidation(
        make_doc_row("2026-04-07", items, item_count=2, merge_operations=1, merged_clusters=1),
        [items[0], items[2]],
        trace,
        ENTITIES,
        ISSUES,
    )
    rows = query(
        repo,
        "SELECT item_id, item_seq, merged, source_count, origin_item_ids_json, "
        "project_entity_id, evidence_mode, evidence_contiguous, title_source "
        "FROM ods_m2_meeting_items ORDER BY item_seq",
    )
    assert [r["item_seq"] for r in rows] == [0, 1]
    assert [r["merged"] for r in rows] == [1, 0]
    assert [r["source_count"] for r in rows] == [2, 1]
    assert [r["evidence_mode"] for r in rows] == ["primary_source", "single"]
    assert [r["title_source"] for r in rows] == ["llm", "source"]
    assert all(r["project_entity_id"] == "P0001" for r in rows)
    assert all(r["evidence_contiguous"] == 1 for r in rows)

    origins = [json.loads(r["origin_item_ids_json"]) for r in rows]
    assert origins[0] == [
        R.derive_origin_item_id("2026-04-07", 0),
        R.derive_origin_item_id("2026-04-07", 1),
    ]
    assert origins[1] == [R.derive_origin_item_id("2026-04-07", 2)]
    assert rows[0]["item_id"] == R.derive_item_id("2026-04-07", 0)


def test_lineage_view_covers_every_source_item_exactly_once(repo):
    """血缘视图必须能 JOIN 回 ods_m1_meeting_items，且不漏不重。"""
    items = [make_item(title="甲"), make_item(title="乙"), make_item(title="丙")]
    seed_m1_document(repo, "2026-04-07", items)
    repo.upsert_consolidation(
        make_doc_row("2026-04-07", items, item_count=2, merge_operations=1),
        [items[0], items[2]],
        make_trace(2, merged_at=(0,)),
        ENTITIES,
        ISSUES,
    )
    joined = query(
        repo,
        "SELECT l.origin_item_id, m.title AS m1_title FROM dwd_m2_item_lineage l "
        "JOIN ods_m1_meeting_items m ON m.item_id = l.origin_item_id "
        "ORDER BY l.origin_item_id",
    )
    assert len(joined) == 3
    orphan = scalar(
        repo,
        "SELECT COUNT(*) FROM dwd_m2_item_lineage l "
        "LEFT JOIN ods_m1_meeting_items m ON m.item_id = l.origin_item_id "
        "WHERE m.item_id IS NULL",
    )
    assert orphan == 0


def test_detail_and_stat_views(repo):
    items = [make_item(), make_item(title="第二条")]
    seed_m1_document(repo, "2026-04-07", items)
    repo.upsert_consolidation(make_doc_row("2026-04-07", items), items, make_trace(2), ENTITIES, ISSUES)

    # 责任人展开：每条 2 个 assignee → 4 行
    assert scalar(repo, "SELECT COUNT(*) FROM dwd_m2_item_detail") == 4
    stat = query(
        repo,
        "SELECT source_document_id, m1_item_count, item_count, merge_rate, validation_status "
        "FROM dws_m2_merge_stat",
    )
    assert stat[0]["m1_item_count"] == 2
    assert stat[0]["item_count"] == 2
    assert stat[0]["merge_rate"] == 0.0
    assert stat[0]["validation_status"] == "REVIEW"
    assert scalar(repo, "SELECT COUNT(*) FROM dwd_m2_project_entity") == 1
    # item_cnt 建在责任人展开视图上，2 条 x 2 个 assignee = 4（与 M1 DWS 同口径）；
    # 要条目本数读 distinct_item_cnt。
    assert scalar(repo, "SELECT SUM(item_cnt) FROM dws_m2_item_stat") == 4
    assert scalar(repo, "SELECT SUM(distinct_item_cnt) FROM dws_m2_item_stat") == 2


def test_stats_counters(repo):
    items = [make_item(), make_item(title="第二条"), make_item(title="第三条")]
    seed_m1_document(repo, "2026-04-07", items)
    repo.upsert_consolidation(
        make_doc_row("2026-04-07", items, item_count=2, merge_operations=1, merged_clusters=1),
        [items[0], items[2]],
        make_trace(2, merged_at=(0,)),
        ENTITIES,
        ISSUES,
    )
    stats = repo.stats("2026-04-07")
    assert stats["item_count"] == 2
    assert stats["merged_count"] == 1
    # 每条 M1 来源都被覆盖：1 条合并吃掉 2 条 + 1 条原样 = 3
    assert stats["source_item_count_covered"] == 3
    assert stats["by_evidence_mode"] == {"primary_source": 1, "single": 1}
    assert stats["issues"] == {"error": 0, "review": 2, "warning": 0}
    assert stats["project_entity_count"] == 1
    assert stats["document"]["validation_status"] == "REVIEW"
    assert stats["document"]["llm_calls"] == 7
    # report_json 落库后应被解回 dict，并把原始字符串列藏起来
    assert stats["document"]["report"] == {"stats": {"output_items": 2}}
    assert "report_json" not in stats["document"]


def test_stats_for_unknown_document(repo):
    stats = repo.stats("不存在")
    assert stats["document"] is None
    assert stats["item_count"] == 0
    assert stats["source_item_count_covered"] == 0


# --------------------------------------------------------------------------
# 红线：对 ods_m1_* / ods_m4_* 只读
# --------------------------------------------------------------------------


def test_repository_never_writes_m1_or_m4_tables():
    """中台数据不得写回流水线：仓储源码里不允许出现对 ods_m1_* / ods_m4_* 的写语句。"""
    source = (M2_SERVICE_DIR / "repository.py").read_text(encoding="utf-8")
    # 剔除注释与文档串后再查，避免被本测试自己的说明文字误伤
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    for verb in ("INSERT INTO", "UPDATE", "DELETE FROM", "REPLACE INTO", "DROP TABLE", "ALTER TABLE"):
        for table in ("ods_m1_", "ods_m4_"):
            pattern = r"{}\s+{}".format(verb, table)
            assert not re.search(pattern, code, re.IGNORECASE), "发现写语句：{}".format(pattern)


def test_sqlite_bootstrap_does_not_touch_existing_m1_rows(repo):
    """bootstrap_schema 用的是 CREATE TABLE IF NOT EXISTS，已有 M1 数据不能被清掉。"""
    items = [make_item()]
    seed_m1_document(repo, "2026-04-07", items)
    repo.bootstrap_schema()
    repo.bootstrap_schema()
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m1_meeting_items") == 1
    assert scalar(repo, "SELECT COUNT(*) FROM ods_m1_source_documents") == 1


def _table_counts(repo):
    return {
        table: scalar(repo, "SELECT COUNT(*) FROM " + table)
        for table in (
            "ods_m2_consolidated_documents",
            "ods_m2_meeting_items",
            "ods_m2_project_entities",
            "ods_m2_validation_issues",
        )
    }
