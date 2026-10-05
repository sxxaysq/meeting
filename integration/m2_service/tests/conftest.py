# -*- coding: utf-8 -*-
"""m2_service 测试公共夹具。

全部测试**不联网、不调 LLM**：runner 走 no_llm=True（M2 的判定全 UNCERTAIN，
链路照常跑通），仓储走 SQLite 自测形态。

两个必须守住的坑：
1. **sys.path 顺序**：runner 导入时会把 `M2_SemanticConsolidator/src` 插到 sys.path[0]，
   而 M2 src 里也有 `cli.py` / `pipeline.py` / `repository` 之外的同名模块。
   每个测试模块 import 本项目模块前，必须把 m2_service 目录重新插回 0 位，
   否则会解析到 M2 src 下的同名文件（m4 已踩过这个坑）。
2. **M2_DATA_DIR 必须在 import runner 之前设置**：DATA_DIR / M2_OUT_DIR /
   CATALOG_PATH 都是 runner 的模块级常量，导入时一次性求值。
"""

import os
import sys
import tempfile
from pathlib import Path

# ---- 必须在任何本项目模块导入之前完成 ------------------------------------
HERE = Path(__file__).resolve().parent
M2_SERVICE_DIR = HERE.parent
INTEGRATION_DIR = M2_SERVICE_DIR.parent
REPO_ROOT = INTEGRATION_DIR.parent

_SCRATCH = Path(tempfile.mkdtemp(prefix="m2_service_tests_"))
os.environ["M2_DATA_DIR"] = str(_SCRATCH / "data")
# 项目主表也得隔离：否则单测会把 entity_id 写进真的 data/project_catalog.sqlite，
# 污染正式归并的实体编号。
os.environ["M2_CATALOG_PATH"] = str(_SCRATCH / "data" / "project_catalog.sqlite")

sys.path.insert(0, str(M2_SERVICE_DIR))

import json  # noqa: E402
import sqlite3  # noqa: E402

import pytest  # noqa: E402


def guard_sys_path() -> None:
    """把 m2_service 目录重新插回 sys.path[0]。

    调用时机：任何测试模块在 import 过 runner（或间接 import 过 M2 src）之后，
    再要 import 本项目的 cli / service / repository 之前。
    """
    while str(M2_SERVICE_DIR) in sys.path:
        sys.path.remove(str(M2_SERVICE_DIR))
    sys.path.insert(0, str(M2_SERVICE_DIR))


def make_item(**overrides):
    """一个完全合法的九字段 item（形态与 m1_staging / m4_bidding 的 conftest 一致）。"""
    item = {
        "department": "智能矿山事业部",
        "work_section": "经营工作",
        "delivery_group": None,
        "project": "智能综合管控平台",
        "item_type": "PROJECT_TASK",
        "assignee": ["张三", "李四"],
        "title": "完成管控平台数据接入",
        "content": "本周完成智能综合管控平台的数据接入与联调",
        "evidence": {
            "text": "完成智能综合管控平台的数据接入与联调",
            "page_start": 1,
            "page_end": 1,
            "start_char": 120,
            "end_char": 138,
            "exact_match": True,
        },
    }
    item.update(overrides)
    return item


def seed_m1_document(repo, source_document_id, items, mode="generic", file_name=None):
    """往自测库的 ods_m1_* 灌一份 M1 输出，模拟数据中台里已有的 M1 资产。

    只在 SQLite 自测形态下用；正式形态这些行由 m1_staging 服务写入，
    m2_service 对 ods_m1_* **只 SELECT**。
    """
    from repository import derive_origin_item_id, utcnow

    now = utcnow()
    file_name = file_name or (source_document_id + ".items.json")
    meeting_date = source_document_id if _is_date(source_document_id) else None
    with repo._lock:
        repo._conn.execute(
            "INSERT OR REPLACE INTO ods_m1_source_documents "
            "(source_document_id, file_name, meeting_date, mode, item_count, ingested_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_document_id, file_name, meeting_date, mode, len(items), now, now),
        )
        repo._conn.execute(
            "DELETE FROM ods_m1_meeting_items WHERE source_document_id = ?",
            (source_document_id,),
        )
        for idx, item in enumerate(items):
            evidence = item["evidence"]
            repo._conn.execute(
                "INSERT INTO ods_m1_meeting_items (item_id, source_document_id, item_seq, "
                "department, work_section, delivery_group, project, item_type, assignees_json, "
                "title, content, evidence_text, evidence_start_char, evidence_end_char, "
                "evidence_page_start, evidence_page_end, exact_match, item_json, "
                "created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    derive_origin_item_id(source_document_id, idx),
                    source_document_id,
                    idx,
                    item.get("department"),
                    item.get("work_section"),
                    item.get("delivery_group"),
                    item.get("project"),
                    item["item_type"],
                    json.dumps(item.get("assignee") or [], ensure_ascii=False),
                    item["title"],
                    item["content"],
                    evidence["text"],
                    evidence["start_char"],
                    evidence["end_char"],
                    evidence.get("page_start"),
                    evidence.get("page_end"),
                    1 if evidence["exact_match"] else 0,
                    json.dumps(item, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        repo._conn.commit()
    return source_document_id


def _is_date(value: str) -> bool:
    from datetime import datetime

    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def query(repo, sql, params=()):
    """直连自测库做断言查询（含视图）。"""
    with repo._lock:
        return [dict(row) for row in repo._conn.execute(sql, params).fetchall()]


def scalar(repo, sql, params=()):
    rows = query(repo, sql, params)
    if not rows:
        return None
    return list(rows[0].values())[0]


@pytest.fixture()
def repo(tmp_path):
    """独立的 SQLite 自测库（自带 ods_m1_* 源表 + ods_m2_* 四表四视图）。"""
    guard_sys_path()
    from repository import SqliteRepository

    repository = SqliteRepository(str(tmp_path / "m2.db"))
    yield repository
    repository.close()


@pytest.fixture()
def catalog_path(tmp_path):
    """每份测试独立的项目主表，避免 entity_id 跨测试串味。"""
    return str(tmp_path / "project_catalog.sqlite")


@pytest.fixture()
def seeded(repo):
    """两份会议、共 5 条 M1 条目：其中 0407 的两条完全同项目同部门（可归并候选）。"""
    guard_sys_path()
    seed_m1_document(
        repo,
        "2026-04-07",
        [
            make_item(title="完成管控平台数据接入"),
            make_item(
                title="完成管控平台数据接入工作",
                content="本周完成智能综合管控平台的数据接入与联调",
                evidence={
                    "text": "本周完成智能综合管控平台的数据接入与联调",
                    "page_start": 1,
                    "page_end": 1,
                    "start_char": 118,
                    "end_char": 138,
                    "exact_match": True,
                },
            ),
            make_item(title="梳理数据标准", project="数据中台项目"),
        ],
    )
    seed_m1_document(
        repo,
        "2026-04-13",
        [
            make_item(title="推进红沙泉二矿立项", project="红沙泉二矿项目"),
            make_item(title="组织安全培训", item_type="NON_PROJECT_WORK", project=None),
        ],
    )
    return ["2026-04-07", "2026-04-13"]
