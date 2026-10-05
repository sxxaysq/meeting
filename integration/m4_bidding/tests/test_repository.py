# -*- coding: utf-8 -*-
"""repository.py / 端到端切分流程单元测试（SQLite 方言，无网络）。"""

import json
import sqlite3
from pathlib import Path

from conftest import FakeLLMClient, make_bidding_item, make_item

from repository import derive_item_id, derive_origin_item_id

DOC = "2026-04-07"


def _annotations(items, bidding_idxs):
    return {
        idx: {
            "idx": idx,
            "is_bidding": True,
            "category": "TENDER",
            "reason": "测试判定",
        }
        for idx in bidding_idxs
    }


def test_upsert_idempotent_and_ids(sqlite_repo):
    items = [make_bidding_item(), make_item(), make_item()]
    annotations = _annotations(items, [0, 2])
    first = sqlite_repo.upsert_bidding(DOC, "2026-04-07.items.json", "2026-04-07",
                                       "generic", items, annotations)
    assert first == {"bidding_count": 2, "item_count": 3}
    second = sqlite_repo.upsert_bidding(DOC, "2026-04-07.items.json", "2026-04-07",
                                        "generic", items, annotations)
    # 幂等：重复 ingest 行数不变
    assert second["bidding_count"] == 2
    stats = sqlite_repo.stats(DOC)
    assert stats["bidding_count"] == 2
    assert stats["by_category"] == {"TENDER": 2}
    assert stats["document"]["item_count"] == 3


def test_row_fields_and_links(sqlite_repo):
    import sqlite3

    items = [make_bidding_item()]
    sqlite_repo.upsert_bidding(DOC, "x.items.json", "2026-04-07", "generic",
                               items, _annotations(items, [0]))
    conn = sqlite3.connect(sqlite_repo.db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM ods_m4_bidding_items").fetchone())
    conn.close()
    assert row["item_id"] == derive_item_id(DOC, 0)
    assert row["origin_item_id"] == derive_origin_item_id(DOC, 0)
    assert row["bidding_category"] == "TENDER"
    # 负责人/项目/部门字段全保留（与 m1 同构、同关联方式）
    assert json.loads(row["assignees_json"]) == ["张三", "李四"]
    assert row["project"] == "红沙泉二矿智能化建设项目"
    assert row["department"] == "智能矿山事业部"
    assert json.loads(row["item_json"]) == items[0]


def test_split_flow_filtered_passes_m1_schema(sqlite_repo, tmp_path):
    """端到端：分类 → 切分 → filtered 必须通过 m1 九字段 Schema。"""
    import jsonschema

    from classifier import classify_items, split_items

    schema_path = (
        Path(__file__).resolve().parents[3] / "M1_Extraction" / "schemas" / "m1_items.schema.json"
    )
    items = [make_bidding_item(), make_item(), make_item()]
    result = classify_items(items, FakeLLMClient({0: (True, "TENDER", "编制招标文件")}))
    split = split_items(items, result["annotations"])
    assert len(split["bidding"]) == 1 and len(split["filtered"]) == 2

    sqlite_repo.upsert_bidding(
        DOC, "x.items.json", "2026-04-07", "generic", items,
        {idx: result["annotations"][idx] for idx in result["bidding_indices"]},
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = list(jsonschema.Draft7Validator(schema).iter_errors({"items": split["filtered"]}))
    assert errors == []


def test_resplit_with_fewer_bidding_clears_stale_rows(sqlite_repo):
    """重分类后招投标条目变少时，必须清掉上一轮的残留行。

    回归背景（2026-09-04 数据集端到端测试实测踩到）：招投标条目是稀疏子集，
    只 upsert 会把上一轮判为招投标、这一轮没判中的行留在表里。实测
    2026-04-20 本轮判 17 条、表里却躺着 19 行。
    """
    items = [make_bidding_item(), make_item(), make_bidding_item()]
    sqlite_repo.upsert_bidding(DOC, "x.items.json", "2026-04-07", "generic",
                               items, _annotations(items, [0, 2]))
    assert sqlite_repo.stats(DOC)["bidding_count"] == 2

    sqlite_repo.upsert_bidding(DOC, "x.items.json", "2026-04-07", "generic",
                               items, _annotations(items, [1]))
    stats = sqlite_repo.stats(DOC)
    assert stats["bidding_count"] == 1

    conn = sqlite3.connect(sqlite_repo.db_path)
    seqs = [r[0] for r in conn.execute(
        "SELECT item_seq FROM ods_m4_bidding_items ORDER BY item_seq")]
    conn.close()
    assert seqs == [1]


def test_resplit_with_same_count_but_different_set_is_replaced(sqlite_repo):
    """更阴的一种：条数巧合相等、但判中的条目换了，只看计数根本发现不了。"""
    items = [make_bidding_item(), make_item(), make_bidding_item()]
    sqlite_repo.upsert_bidding(DOC, "x.items.json", "2026-04-07", "generic",
                               items, _annotations(items, [0, 2]))
    sqlite_repo.upsert_bidding(DOC, "x.items.json", "2026-04-07", "generic",
                               items, _annotations(items, [0, 1]))
    assert sqlite_repo.stats(DOC)["bidding_count"] == 2

    conn = sqlite3.connect(sqlite_repo.db_path)
    seqs = [r[0] for r in conn.execute(
        "SELECT item_seq FROM ods_m4_bidding_items ORDER BY item_seq")]
    conn.close()
    # 必须恰好是本轮的 {0,1}，不能残留上一轮的 2
    assert seqs == [0, 1]


def test_resplit_with_no_bidding_empties_the_document(sqlite_repo):
    """本轮一条招投标都没判中 → 该文档在表里应当清空，而不是留旧行。"""
    items = [make_bidding_item(), make_item()]
    sqlite_repo.upsert_bidding(DOC, "x.items.json", "2026-04-07", "generic",
                               items, _annotations(items, [0]))
    assert sqlite_repo.stats(DOC)["bidding_count"] == 1

    sqlite_repo.upsert_bidding(DOC, "x.items.json", "2026-04-07", "generic", items, {})
    assert sqlite_repo.stats(DOC)["bidding_count"] == 0
    # 文档行仍在（记录本轮 m1 总数），只是没有招投标条目
    assert sqlite_repo.stats(DOC)["document"]["item_count"] == 2


# --------------------------------------------------------------------------
# doc id 归一：向 ods_m1_source_documents 已登记的口径对齐
# --------------------------------------------------------------------------
# 回归背景（2026-09-04 数据集端到端测试实测踩到）：8.24 那份 M1 登记的 doc id 是
# 文件名形态 `2026.8.24信息公司周例会工作安排备忘录`，而 M4 当年试跑用的是规范化的
# `2026-08-24`。两边各写各的，M4 的 origin_item_id = sha1("2026-08-24#idx") 根本
# JOIN 不回 M1 的 sha1("2026.8.24信息…#idx")，血缘断掉。

M1_DOC_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS ods_m1_source_documents (
  source_document_id TEXT PRIMARY KEY,
  file_name   TEXT NOT NULL,
  meeting_date TEXT NULL,
  mode        TEXT NOT NULL DEFAULT 'generic',
  item_count  INTEGER NOT NULL DEFAULT 0,
  ingested_at TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (file_name)
)
"""

FILENAME_FORM = "2026.8.24信息公司周例会工作安排备忘录"


def _seed_m1_docs(sqlite_repo, rows):
    """在自测库里建出 ods_m1_source_documents 并灌入 M1 已登记的文档。

    m4 自己的 SQLite DDL 只建 ods_m4_*，所以默认没有这张参照表——
    这正好也是"无参照表时不归一"那条用例的前提。
    """
    with sqlite_repo._lock:
        sqlite_repo._conn.execute(M1_DOC_SQLITE_DDL)
        for doc_id, meeting_date in rows:
            sqlite_repo._conn.execute(
                "INSERT OR REPLACE INTO ods_m1_source_documents "
                "(source_document_id, file_name, meeting_date, mode, item_count, "
                " ingested_at, updated_at) VALUES (?, ?, ?, 'generic', 0, ?, ?)",
                (doc_id, doc_id + ".items.json", meeting_date,
                 repository_utcnow(), repository_utcnow()),
            )
        sqlite_repo._conn.commit()


def repository_utcnow():
    from repository import utcnow

    return utcnow()


def test_date_from_doc_id_variants():
    from repository import date_from_doc_id

    assert date_from_doc_id("2026-08-24") == "2026-08-24"
    assert date_from_doc_id("2026.8.24") == "2026-08-24"
    assert date_from_doc_id("2026.8.4") == "2026-08-04"
    assert date_from_doc_id("2026/8/24") == "2026-08-24"
    # 文件名形态：日期后面跟着中文，也要能确定性取出
    assert date_from_doc_id(FILENAME_FORM) == "2026-08-24"
    assert date_from_doc_id(FILENAME_FORM + ".pdf") == "2026-08-24"
    # m1_service /m1/extract 返回的临时身份形态
    assert date_from_doc_id("doc:" + FILENAME_FORM + ".pdf") == "2026-08-24"


def test_date_from_doc_id_does_not_guess():
    from repository import date_from_doc_id

    assert date_from_doc_id("不存在的会议") is None
    assert date_from_doc_id("") is None
    assert date_from_doc_id(None) is None
    # 非法月日不猜，直接 None
    assert date_from_doc_id("2026-13-45") is None
    assert date_from_doc_id("2026.2.30") is None


def test_resolve_without_m1_reference_table_returns_input(sqlite_repo):
    """自测库没有 ods_m1_* 参照表：M4 允许脱离 M1 独立跑，此时不归一、不报错。"""
    assert sqlite_repo.resolve_source_document_id("2026-08-24") == "2026-08-24"
    assert sqlite_repo.resolve_source_document_id(FILENAME_FORM) == FILENAME_FORM


def test_resolve_prefers_exact_registered_id(sqlite_repo):
    _seed_m1_docs(sqlite_repo, [("2026-04-07", "2026-04-07")])
    assert sqlite_repo.resolve_source_document_id("2026-04-07") == "2026-04-07"


def test_resolve_date_form_to_registered_filename_form(sqlite_repo):
    """核心用例：传规范日期，要能解析到 M1 实际登记的文件名形态 id。"""
    _seed_m1_docs(sqlite_repo, [(FILENAME_FORM, "2026-08-24")])
    assert sqlite_repo.resolve_source_document_id("2026-08-24") == FILENAME_FORM
    # 文件名形态本身精确命中，也应原样返回
    assert sqlite_repo.resolve_source_document_id(FILENAME_FORM) == FILENAME_FORM
    # 带 .pdf 后缀 / doc: 前缀的形态同样能落到已登记 id
    assert sqlite_repo.resolve_source_document_id(FILENAME_FORM + ".pdf") == FILENAME_FORM
    assert sqlite_repo.resolve_source_document_id("doc:" + FILENAME_FORM + ".pdf") == FILENAME_FORM


def test_resolve_filename_form_to_registered_date_form(sqlite_repo):
    """反方向：M1 登记的是规范日期，传进来的是文件名形态。"""
    _seed_m1_docs(sqlite_repo, [("2026-08-24", "2026-08-24")])
    assert sqlite_repo.resolve_source_document_id(FILENAME_FORM) == "2026-08-24"
    assert sqlite_repo.resolve_source_document_id(FILENAME_FORM + ".pdf") == "2026-08-24"


def test_resolve_ambiguous_date_returns_input(sqlite_repo):
    """同一天对应多份已登记文档、且没有一份的 id 就等于规范日期 → 歧义，不猜。

    注意造数据时不能让任何一份的 id 恰好是 `2026-08-24`，否则会先被
    “归一后的精确命中”那一步拦下（那是另一个用例，见下一条）。
    """
    _seed_m1_docs(sqlite_repo, [
        ("2026.8.24上午会", "2026-08-24"),
        ("2026.8.24下午会", "2026-08-24"),
    ])
    assert sqlite_repo.resolve_source_document_id("2026-08-24") == "2026-08-24"
    assert sqlite_repo.resolve_source_document_id("2026.8.24别的会议") == "2026.8.24别的会议"


def test_resolve_exact_hit_on_normalized_date_wins_over_ambiguity(sqlite_repo):
    """优先级固化：归一后的日期形态如果在 M1 里精确命中，就直接用它，
    即使同一天还登记了其他文档（精确命中不属歧义）。"""
    _seed_m1_docs(sqlite_repo, [
        ("2026-08-24", "2026-08-24"),
        (FILENAME_FORM, "2026-08-24"),
    ])
    assert sqlite_repo.resolve_source_document_id("2026.8.24别的会议") == "2026-08-24"


def test_resolve_unknown_document_returns_input(sqlite_repo):
    """M1 里查无此文档：原样返回（M4 可以先于 M1 入库跑）。"""
    _seed_m1_docs(sqlite_repo, [("2026-04-07", "2026-04-07")])
    assert sqlite_repo.resolve_source_document_id("1999-01-01") == "1999-01-01"
    assert sqlite_repo.resolve_source_document_id("没有日期的会议") == "没有日期的会议"


def test_resolve_empty_input(sqlite_repo):
    assert sqlite_repo.resolve_source_document_id("") == ""
    assert sqlite_repo.resolve_source_document_id(None) is None


def test_split_writes_under_resolved_m1_doc_id(sqlite_repo):
    """端到端：传 2026-08-24 进来，落库必须落在 M1 登记的 id 上，
    且 origin_item_id 用同一个 id 派生，血缘才 JOIN 得回 M1。"""
    from repository import derive_origin_item_id

    _seed_m1_docs(sqlite_repo, [(FILENAME_FORM, "2026-08-24")])
    items = [make_bidding_item(), make_item()]
    sqlite_repo.upsert_bidding(
        sqlite_repo.resolve_source_document_id("2026-08-24"),
        "2026.8.24信息公司周例会工作安排备忘录.pdf", "2026-08-24", "generic",
        items, _annotations(items, [0]),
    )
    # 落在 M1 口径下，而不是 2026-08-24
    assert sqlite_repo.stats(FILENAME_FORM)["bidding_count"] == 1
    assert sqlite_repo.stats("2026-08-24")["bidding_count"] == 0

    conn = sqlite3.connect(sqlite_repo.db_path)
    row = conn.execute(
        "SELECT source_document_id, origin_item_id FROM ods_m4_bidding_items"
    ).fetchone()
    conn.close()
    assert row[0] == FILENAME_FORM
    # 用 M1 的 doc id 派生 → 可与 ods_m1_meeting_items.item_id 对齐
    assert row[1] == derive_origin_item_id(FILENAME_FORM, 0)
