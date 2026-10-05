# -*- coding: utf-8 -*-
"""M2 staging 仓储层：以 repository 模式隔离 MySQL / SQLite 方言。

与 m1_staging/repository.py、m4_bidding/repository.py 同构，但多一条**读侧**职责：

- 读侧：只 SELECT `ods_m1_source_documents` / `ods_m1_meeting_items`，
  从 `item_json` 列无损还原 M1 九条目，作为 M2 的输入。
  **本模块对 ods_m1_* / ods_m4_* 零写入**（红线：中台数据不得写回流水线）。
- 写侧：单事务 upsert 进 `ods_m2_*` 四表（MySQL 正式形态 / SQLite 自测形态）。

DSN 形态：
    mysql://user:YOUR_PASSWORD@host:port/dbname
    sqlite:////absolute/path/m2_staging.db
    sqlite:///relative/path/m2_staging.db   （相对 integration/m2_service/ 目录）

SQLite 自测形态是独立库文件，因此会**同时**建 ods_m1_* 两张源表
（CREATE TABLE IF NOT EXISTS，仅供单测灌入 M1 输入）；
MySQL 形态下 ods_m1_* 由 m1_staging 建好，bootstrap_schema() 只建 ods_m2_* 与其视图。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
SCHEMA_SQL = HERE / "schema.sql"

# --------------------------------------------------------------------------
# 列定义
# --------------------------------------------------------------------------

_DOC_COLUMNS = [
    "source_document_id",
    "file_name",
    "meeting_date",
    "source_mode",
    "m1_item_count",
    "item_count",
    "merge_operations",
    "merged_clusters",
    "project_entity_count",
    "validation_status",
    "issue_count",
    "input_fingerprint",
    "llm_calls",
    "elapsed_ms",
    "report_json",
]

_ITEM_COLUMNS = [
    "item_id",
    "source_document_id",
    "item_seq",
    "department",
    "work_section",
    "delivery_group",
    "project",
    "item_type",
    "assignees_json",
    "title",
    "content",
    "evidence_text",
    "evidence_start_char",
    "evidence_end_char",
    "evidence_page_start",
    "evidence_page_end",
    "exact_match",
    "item_json",
    "merged",
    "source_count",
    "origin_item_ids_json",
    "project_entity_id",
    "evidence_mode",
    "evidence_contiguous",
    "title_source",
]

_ENTITY_COLUMNS = [
    "source_document_id",
    "entity_id",
    "canonical_name",
    "aliases_json",
    "source_names_json",
    "decisions_json",
]

_ISSUE_COLUMNS = [
    "source_document_id",
    "code",
    "level",
    "message",
    "item_index",
    "detail_json",
]

# --------------------------------------------------------------------------
# SQLite DDL（自测形态；与 schema.sql §B 留档一致）
# --------------------------------------------------------------------------

# 自测库是独立文件，需要自带 M1 源表才能灌入输入。**只建不写**。
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

M1_ITEM_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS ods_m1_meeting_items (
  item_id            TEXT PRIMARY KEY,
  source_document_id TEXT NOT NULL,
  item_seq           INTEGER NOT NULL,
  department         TEXT NULL,
  work_section       TEXT NULL,
  delivery_group     TEXT NULL,
  project            TEXT NULL,
  item_type          TEXT NOT NULL,
  assignees_json     TEXT NOT NULL,
  title              TEXT NOT NULL,
  content            TEXT NOT NULL,
  evidence_text      TEXT NOT NULL,
  evidence_start_char INTEGER NOT NULL,
  evidence_end_char   INTEGER NOT NULL,
  evidence_page_start INTEGER NULL,
  evidence_page_end   INTEGER NULL,
  exact_match        INTEGER NOT NULL,
  item_json          TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (source_document_id, item_seq)
)
"""

DOC_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS ods_m2_consolidated_documents (
  source_document_id   TEXT PRIMARY KEY,
  file_name            TEXT NOT NULL,
  meeting_date         TEXT NULL,
  source_mode          TEXT NOT NULL,
  m1_item_count        INTEGER NOT NULL DEFAULT 0,
  item_count           INTEGER NOT NULL DEFAULT 0,
  merge_operations     INTEGER NOT NULL DEFAULT 0,
  merged_clusters      INTEGER NOT NULL DEFAULT 0,
  project_entity_count INTEGER NOT NULL DEFAULT 0,
  validation_status    TEXT NOT NULL,
  issue_count          INTEGER NOT NULL DEFAULT 0,
  input_fingerprint    TEXT NOT NULL,
  llm_calls            INTEGER NOT NULL DEFAULT 0,
  elapsed_ms           INTEGER NOT NULL DEFAULT 0,
  report_json          TEXT NULL,
  ingested_at TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (file_name)
)
"""

ITEM_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS ods_m2_meeting_items (
  item_id            TEXT PRIMARY KEY,
  source_document_id TEXT NOT NULL,
  item_seq           INTEGER NOT NULL,
  department         TEXT NULL,
  work_section       TEXT NULL,
  delivery_group     TEXT NULL,
  project            TEXT NULL,
  item_type          TEXT NOT NULL,
  assignees_json     TEXT NOT NULL,
  title              TEXT NOT NULL,
  content            TEXT NOT NULL,
  evidence_text      TEXT NOT NULL,
  evidence_start_char INTEGER NOT NULL,
  evidence_end_char   INTEGER NOT NULL,
  evidence_page_start INTEGER NULL,
  evidence_page_end   INTEGER NULL,
  exact_match        INTEGER NOT NULL,
  item_json          TEXT NOT NULL,
  merged              INTEGER NOT NULL,
  source_count        INTEGER NOT NULL,
  origin_item_ids_json TEXT NOT NULL,
  project_entity_id   TEXT NULL,
  evidence_mode       TEXT NOT NULL,
  evidence_contiguous INTEGER NOT NULL,
  title_source        TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (source_document_id, item_seq)
)
"""

ENTITY_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS ods_m2_project_entities (
  source_document_id TEXT NOT NULL,
  entity_id          TEXT NOT NULL,
  canonical_name     TEXT NOT NULL,
  aliases_json       TEXT NOT NULL,
  source_names_json  TEXT NOT NULL,
  decisions_json     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (source_document_id, entity_id)
)
"""

ISSUE_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS ods_m2_validation_issues (
  issue_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source_document_id TEXT NOT NULL,
  code               TEXT NOT NULL,
  level              TEXT NOT NULL,
  message            TEXT NOT NULL,
  item_index         INTEGER NULL,
  detail_json        TEXT NULL,
  created_at TEXT NOT NULL
)
"""

INDEX_SQLITE = [
    "CREATE INDEX IF NOT EXISTS idx_m2_project ON ods_m2_meeting_items (project)",
    "CREATE INDEX IF NOT EXISTS idx_m2_dept ON ods_m2_meeting_items (department)",
    "CREATE INDEX IF NOT EXISTS idx_m2_type ON ods_m2_meeting_items (item_type)",
    "CREATE INDEX IF NOT EXISTS idx_m2_entity ON ods_m2_meeting_items (project_entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_m2_merged ON ods_m2_meeting_items (merged)",
    "CREATE INDEX IF NOT EXISTS idx_m2_status ON ods_m2_consolidated_documents (validation_status)",
    "CREATE INDEX IF NOT EXISTS idx_m2_ent_entity ON ods_m2_project_entities (entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_m2_issue_doc ON ods_m2_validation_issues (source_document_id)",
    "CREATE INDEX IF NOT EXISTS idx_m2_issue_code ON ods_m2_validation_issues (code)",
]

VIEW_SQLITE = [
    "DROP VIEW IF EXISTS dwd_m2_item_detail",
    """CREATE VIEW dwd_m2_item_detail AS
SELECT i.item_id, i.source_document_id, d.meeting_date, d.source_mode,
       i.department, i.work_section, i.delivery_group, i.project, i.item_type,
       i.project_entity_id, i.title, i.content, a.value AS assignee,
       i.merged, i.source_count, i.evidence_mode, i.evidence_contiguous, i.title_source,
       i.evidence_text, i.evidence_start_char, i.evidence_end_char, i.exact_match
FROM ods_m2_meeting_items i
LEFT JOIN ods_m2_consolidated_documents d ON d.source_document_id = i.source_document_id
LEFT JOIN json_each(i.assignees_json) a ON 1 = 1""",
    "DROP VIEW IF EXISTS dwd_m2_item_lineage",
    """CREATE VIEW dwd_m2_item_lineage AS
SELECT i.source_document_id, i.item_id AS m2_item_id, i.item_seq AS m2_item_seq,
       i.merged, i.source_count, l.value AS origin_item_id
FROM ods_m2_meeting_items i JOIN json_each(i.origin_item_ids_json) l ON 1 = 1""",
    "DROP VIEW IF EXISTS dwd_m2_project_entity",
    """CREATE VIEW dwd_m2_project_entity AS
SELECT entity_id, MIN(canonical_name) AS canonical_name,
       COUNT(DISTINCT source_document_id) AS document_cnt,
       JSON_GROUP_ARRAY(canonical_name) AS canonical_name_variants
FROM ods_m2_project_entities GROUP BY entity_id""",
    "DROP VIEW IF EXISTS dws_m2_item_stat",
    """CREATE VIEW dws_m2_item_stat AS
SELECT meeting_date, department, project, item_type,
       COUNT(*) AS item_cnt, COUNT(DISTINCT item_id) AS distinct_item_cnt,
       SUM(exact_match) AS exact_cnt, SUM(merged) AS merged_cnt
FROM dwd_m2_item_detail GROUP BY meeting_date, department, project, item_type""",
    "DROP VIEW IF EXISTS dws_m2_merge_stat",
    """CREATE VIEW dws_m2_merge_stat AS
SELECT source_document_id, file_name, meeting_date, source_mode,
       m1_item_count, item_count, merge_operations, merged_clusters,
       ROUND(CAST(merge_operations AS REAL) / NULLIF(m1_item_count, 0), 4) AS merge_rate,
       project_entity_count, validation_status, issue_count,
       llm_calls, elapsed_ms, ingested_at, updated_at
FROM ods_m2_consolidated_documents""",
]


# --------------------------------------------------------------------------
# 纯函数：ID 派生 / 指纹 / 行组装
# --------------------------------------------------------------------------


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def derive_item_id(source_document_id: str, idx: int) -> str:
    """M2 输出条目的稳定 ID：与 m1/m4 同构，只换前缀。"""
    digest = hashlib.sha1("{}#{}".format(source_document_id, idx).encode("utf-8")).hexdigest()
    return "item:m2:" + digest


def derive_origin_item_id(source_document_id: str, idx: int) -> str:
    """回链 M1 的 item_id。**必须**与 m1_staging/repository.py:derive_item_id 逐字节一致，
    否则 dwd_m2_item_lineage JOIN 回 ods_m1_meeting_items 会全空。"""
    digest = hashlib.sha1("{}#{}".format(source_document_id, idx).encode("utf-8")).hexdigest()
    return "item:m1:" + digest


def meeting_date_from_doc_id(source_document_id: str) -> Optional[str]:
    """会议日期兜底：doc id 形如 YYYY-MM-DD 才解析，非日期形态不猜测。"""
    try:
        return datetime.strptime(source_document_id, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def fingerprint_items(items: List[dict]) -> str:
    """输入指纹：M1 条目列表的内容哈希，用于增量判定（M1 未变则跳过重跑）。

    先 parse 再 sort_keys 重序列化，避免 MySQL JSON 列与 SQLite TEXT 列的
    序列化差异（空格/键序）导致同一份数据算出不同指纹。
    """
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def _date_to_str(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return str(value)


def m2_item_to_row(source_document_id: str, output_idx: int, item: dict, trace: dict) -> dict:
    """把一条 M2 输出 item + 对应 merge_trace 摊平成表行。

    ``trace`` 来自 M2 的 merge_trace，与 items 按 item_index 一一对应；
    origin_item_ids 由 trace.source_indexes 派生回 M1 的 item_id。
    """
    evidence = item["evidence"]
    source_indexes = list(trace.get("source_indexes") or [])
    return {
        "item_id": derive_item_id(source_document_id, output_idx),
        "source_document_id": source_document_id,
        "item_seq": output_idx,
        "department": item.get("department"),
        "work_section": item.get("work_section"),
        "delivery_group": item.get("delivery_group"),
        "project": item.get("project"),
        "item_type": item["item_type"],
        "assignees_json": json.dumps(item.get("assignee") or [], ensure_ascii=False),
        "title": item["title"],
        "content": item["content"],
        "evidence_text": evidence["text"],
        "evidence_start_char": evidence["start_char"],
        "evidence_end_char": evidence["end_char"],
        "evidence_page_start": evidence.get("page_start"),
        "evidence_page_end": evidence.get("page_end"),
        "exact_match": 1 if evidence["exact_match"] else 0,
        "item_json": json.dumps(item, ensure_ascii=False),
        "merged": 1 if trace.get("merged") else 0,
        "source_count": len(source_indexes) or 1,
        "origin_item_ids_json": json.dumps(
            [derive_origin_item_id(source_document_id, i) for i in source_indexes],
            ensure_ascii=False,
        ),
        "project_entity_id": trace.get("project_entity_id"),
        "evidence_mode": trace.get("evidence_mode") or "single",
        "evidence_contiguous": 1 if trace.get("evidence_contiguous") else 0,
        "title_source": trace.get("title_source") or "source",
    }


def entity_to_row(source_document_id: str, entity: dict) -> dict:
    return {
        "source_document_id": source_document_id,
        "entity_id": entity["entity_id"],
        "canonical_name": entity["canonical_name"],
        "aliases_json": json.dumps(entity.get("aliases") or [], ensure_ascii=False),
        "source_names_json": json.dumps(entity.get("source_names") or [], ensure_ascii=False),
        "decisions_json": json.dumps(entity.get("decisions") or [], ensure_ascii=False),
    }


def issue_to_row(source_document_id: str, issue: dict) -> dict:
    detail = issue.get("detail")
    return {
        "source_document_id": source_document_id,
        "code": issue["code"],
        "level": issue["level"],
        "message": issue["message"][:1024],
        "item_index": issue.get("item_index"),
        "detail_json": json.dumps(detail, ensure_ascii=False) if detail else None,
    }


def load_mysql_ddl(schema_path: Optional[Path] = None) -> List[str]:
    """从 schema.sql 抽出 §A（MySQL 8）段落的语句，供 bootstrap-schema 幂等执行。

    单一来源：DDL 只写在 schema.sql 里，这里不重复一份，避免两处漂移。
    段落边界用 §A / §B 的中文小节标记（按**整行**定位，避免标题行残留文字
    被并进第一条 CREATE TABLE），注释行整行剔除后按 ';' 切分。
    """
    path = Path(schema_path or SCHEMA_SQL)
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "§ A. MySQL 8 版" in line)
    end = next(i for i, line in enumerate(lines) if "§ B. SQLite 自测版" in line)
    body = "\n".join(
        line for line in lines[start + 1 : end] if not line.strip().startswith("--")
    )
    return [stmt.strip() for stmt in body.split(";") if stmt.strip()]


# --------------------------------------------------------------------------
# 抽象接口
# --------------------------------------------------------------------------


class M2Repository:
    """方言无关的接口约定：读 ods_m1_* 只 SELECT，写 ods_m2_* 单事务幂等。"""

    dialect = "abstract"

    # ---- 读侧（M1 输出 = M2 输入）--------------------------------
    def list_documents(self) -> List[dict]:
        raise NotImplementedError

    def fetch_items(self, source_document_id: str) -> List[dict]:
        raise NotImplementedError

    def fetch_document(self, source_document_id: str) -> Optional[dict]:
        raise NotImplementedError

    def list_pending(self, limit: Optional[int] = None) -> List[dict]:
        raise NotImplementedError

    # ---- 写侧 ----------------------------------------------------
    def upsert_consolidation(
        self,
        doc_row: dict,
        items: List[dict],
        trace: List[dict],
        project_entities: List[dict],
        issues: List[dict],
    ) -> dict:
        raise NotImplementedError

    def stats(self, source_document_id: str) -> dict:
        raise NotImplementedError

    def bootstrap_schema(self) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# 公共：待归并判定 / 统计组装（与方言无关）
# --------------------------------------------------------------------------


def _build_pending(
    documents: List[dict],
    items_by_doc: Dict[str, List[dict]],
    fingerprints: Dict[str, str],
    limit: Optional[int],
) -> List[dict]:
    pending: List[dict] = []
    for doc in documents:
        doc_id = doc["source_document_id"]
        items = items_by_doc.get(doc_id) or []
        if not items:
            # M1 侧登记了文档但没有条目：不是 M2 能处理的输入，跳过而不是报错
            continue
        digest = fingerprint_items(items)
        known = fingerprints.get(doc_id)
        if known == digest:
            continue
        pending.append(
            {
                "source_document_id": doc_id,
                "file_name": doc.get("file_name"),
                "meeting_date": doc.get("meeting_date"),
                "mode": doc.get("mode"),
                "m1_item_count": len(items),
                "reason": "m1_updated" if known else "never_consolidated",
            }
        )
        if limit is not None and len(pending) >= limit:
            break
    return pending


def _build_stats(doc: Optional[dict], counters: dict) -> dict:
    return {
        "source_document_id": (doc or {}).get("source_document_id"),
        "document": doc,
        "item_count": counters["item_count"],
        "merged_count": counters["merged_count"],
        "source_item_count_covered": counters["source_item_count_covered"],
        "by_item_type": counters["by_item_type"],
        "by_evidence_mode": counters["by_evidence_mode"],
        "issues": counters["issues"],
        "project_entity_count": counters["project_entity_count"],
    }


_EMPTY_COUNTERS = {
    "item_count": 0,
    "merged_count": 0,
    "source_item_count_covered": 0,
    "by_item_type": {},
    "by_evidence_mode": {},
    "issues": {"error": 0, "review": 0, "warning": 0},
    "project_entity_count": 0,
}


def _row_to_doc(row: Any) -> Optional[dict]:
    if row is None:
        return None
    doc = dict(row)
    doc["meeting_date"] = _date_to_str(doc.get("meeting_date"))
    if doc.get("report_json"):
        raw = doc["report_json"]
        try:
            doc["report"] = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            doc["report"] = None
    doc.pop("report_json", None)
    for key in ("ingested_at", "updated_at"):
        doc[key] = str(doc[key]) if doc.get(key) is not None else None
    return doc


# --------------------------------------------------------------------------
# SQLite 形态
# --------------------------------------------------------------------------


class SqliteRepository(M2Repository):
    """自测/单机适配层。进程内单连接 + 锁，语义与 MySQL 版一致。"""

    dialect = "sqlite"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.bootstrap_schema()

    # ---- schema --------------------------------------------------
    def bootstrap_schema(self) -> dict:
        with self._lock:
            # 自测库需要自带 M1 源表才能灌输入；只建不写。
            self._conn.execute(M1_DOC_SQLITE_DDL)
            self._conn.execute(M1_ITEM_SQLITE_DDL)
            self._conn.execute(DOC_SQLITE_DDL)
            self._conn.execute(ITEM_SQLITE_DDL)
            self._conn.execute(ENTITY_SQLITE_DDL)
            self._conn.execute(ISSUE_SQLITE_DDL)
            for stmt in INDEX_SQLITE:
                self._conn.execute(stmt)
            for stmt in VIEW_SQLITE:
                self._conn.execute(stmt)
            self._conn.commit()
        return {"dialect": self.dialect, "tables": 6, "views": 5}

    # ---- 读侧 ----------------------------------------------------
    def list_documents(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_document_id, file_name, meeting_date, mode, item_count "
                "FROM ods_m1_source_documents ORDER BY source_document_id"
            ).fetchall()
        return [
            {
                "source_document_id": r["source_document_id"],
                "file_name": r["file_name"],
                "meeting_date": _date_to_str(r["meeting_date"]),
                "mode": r["mode"],
                "item_count": int(r["item_count"]),
            }
            for r in rows
        ]

    def fetch_items(self, source_document_id: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_json FROM ods_m1_meeting_items "
                "WHERE source_document_id = ? ORDER BY item_seq",
                (source_document_id,),
            ).fetchall()
        return [json.loads(r["item_json"]) for r in rows]

    def _fetch_all_items_grouped(self) -> tuple:
        """一次捞全量 M1 条目（2077 行量级）与已归并指纹，供增量判定。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_document_id, item_json FROM ods_m1_meeting_items "
                "ORDER BY source_document_id, item_seq"
            ).fetchall()
            known = self._conn.execute(
                "SELECT source_document_id, input_fingerprint FROM ods_m2_consolidated_documents"
            ).fetchall()
        grouped: Dict[str, List[dict]] = {}
        for row in rows:
            grouped.setdefault(row["source_document_id"], []).append(json.loads(row["item_json"]))
        fingerprints = {r["source_document_id"]: r["input_fingerprint"] for r in known}
        return grouped, fingerprints

    def fetch_document(self, source_document_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT source_document_id, file_name, meeting_date, mode, item_count "
                "FROM ods_m1_source_documents WHERE source_document_id = ?",
                (source_document_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "source_document_id": row["source_document_id"],
            "file_name": row["file_name"],
            "meeting_date": _date_to_str(row["meeting_date"]),
            "mode": row["mode"],
            "item_count": int(row["item_count"]),
            "items": self.fetch_items(source_document_id),
        }

    def list_pending(self, limit: Optional[int] = None) -> List[dict]:
        grouped, fingerprints = self._fetch_all_items_grouped()
        return _build_pending(self.list_documents(), grouped, fingerprints, limit)

    # ---- 写侧 ----------------------------------------------------
    def upsert_consolidation(
        self,
        doc_row: dict,
        items: List[dict],
        trace: List[dict],
        project_entities: List[dict],
        issues: List[dict],
    ) -> dict:
        now = utcnow()
        doc_id = doc_row["source_document_id"]
        rows = [m2_item_to_row(doc_id, idx, item, trace[idx]) for idx, item in enumerate(items)]

        doc_placeholders = ", ".join("?" for _ in _DOC_COLUMNS)
        doc_updates = ", ".join(
            "{}=excluded.{}".format(col, col) for col in _DOC_COLUMNS if col != "source_document_id"
        ) + ", updated_at=excluded.updated_at"
        doc_sql = (
            "INSERT INTO ods_m2_consolidated_documents ({}, ingested_at, updated_at) "
            "VALUES ({}, ?, ?) ON CONFLICT(source_document_id) DO UPDATE SET {}".format(
                ", ".join(_DOC_COLUMNS), doc_placeholders, doc_updates
            )
        )

        item_placeholders = ", ".join("?" for _ in _ITEM_COLUMNS)
        item_updates = ", ".join(
            "{}=excluded.{}".format(col, col) for col in _ITEM_COLUMNS if col != "item_id"
        ) + ", updated_at=excluded.updated_at"
        item_sql = (
            "INSERT INTO ods_m2_meeting_items ({}, created_at, updated_at) "
            "VALUES ({}, ?, ?) ON CONFLICT(item_id) DO UPDATE SET {}".format(
                ", ".join(_ITEM_COLUMNS), item_placeholders, item_updates
            )
        )

        entity_sql = (
            "INSERT INTO ods_m2_project_entities ({}, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_document_id, entity_id) DO UPDATE SET "
            "canonical_name=excluded.canonical_name, aliases_json=excluded.aliases_json, "
            "source_names_json=excluded.source_names_json, decisions_json=excluded.decisions_json, "
            "updated_at=excluded.updated_at".format(", ".join(_ENTITY_COLUMNS))
        )
        issue_sql = "INSERT INTO ods_m2_validation_issues ({}, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)".format(
            ", ".join(_ISSUE_COLUMNS)
        )

        with self._lock:
            try:
                cursor = self._conn.cursor()
                cursor.execute(
                    doc_sql,
                    [doc_row[col] for col in _DOC_COLUMNS] + [now, now],
                )
                # 条目：先清 stale 尾行（重跑后条目变少时不残留），再逐行 upsert
                cursor.execute(
                    "DELETE FROM ods_m2_meeting_items WHERE source_document_id = ? AND item_seq >= ?",
                    (doc_id, len(rows)),
                )
                for row in rows:
                    cursor.execute(item_sql, [row[col] for col in _ITEM_COLUMNS] + [now, now])
                # 子表无跨次稳定键：同事务内先删后插
                cursor.execute(
                    "DELETE FROM ods_m2_project_entities WHERE source_document_id = ?", (doc_id,)
                )
                for entity in project_entities:
                    erow = entity_to_row(doc_id, entity)
                    cursor.execute(
                        entity_sql, [erow[col] for col in _ENTITY_COLUMNS] + [now, now]
                    )
                cursor.execute(
                    "DELETE FROM ods_m2_validation_issues WHERE source_document_id = ?", (doc_id,)
                )
                for issue in issues:
                    irow = issue_to_row(doc_id, issue)
                    cursor.execute(issue_sql, [irow[col] for col in _ISSUE_COLUMNS] + [now])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "source_document_id": doc_id,
            "item_count": len(rows),
            "project_entity_count": len(project_entities),
            "issue_count": len(issues),
        }

    def stats(self, source_document_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ods_m2_consolidated_documents WHERE source_document_id = ?",
                (source_document_id,),
            ).fetchone()
            counters = dict(_EMPTY_COUNTERS)
            counters["by_item_type"] = {}
            counters["by_evidence_mode"] = {}
            counters["issues"] = dict(_EMPTY_COUNTERS["issues"])
            agg = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(merged), 0) AS m, "
                "COALESCE(SUM(source_count), 0) AS s FROM ods_m2_meeting_items "
                "WHERE source_document_id = ?",
                (source_document_id,),
            ).fetchone()
            by_type = self._conn.execute(
                "SELECT item_type, COUNT(*) AS cnt FROM ods_m2_meeting_items "
                "WHERE source_document_id = ? GROUP BY item_type",
                (source_document_id,),
            ).fetchall()
            by_mode = self._conn.execute(
                "SELECT evidence_mode, COUNT(*) AS cnt FROM ods_m2_meeting_items "
                "WHERE source_document_id = ? GROUP BY evidence_mode",
                (source_document_id,),
            ).fetchall()
            by_level = self._conn.execute(
                "SELECT level, COUNT(*) AS cnt FROM ods_m2_validation_issues "
                "WHERE source_document_id = ? GROUP BY level",
                (source_document_id,),
            ).fetchall()
            ent = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM ods_m2_project_entities WHERE source_document_id = ?",
                (source_document_id,),
            ).fetchone()
        counters["item_count"] = int(agg["n"] or 0)
        counters["merged_count"] = int(agg["m"] or 0)
        counters["source_item_count_covered"] = int(agg["s"] or 0)
        counters["by_item_type"] = {r["item_type"]: int(r["cnt"]) for r in by_type}
        counters["by_evidence_mode"] = {r["evidence_mode"]: int(r["cnt"]) for r in by_mode}
        for r in by_level:
            counters["issues"][r["level"]] = int(r["cnt"])
        counters["project_entity_count"] = int(ent["cnt"] or 0)
        stats = _build_stats(_row_to_doc(row), counters)
        stats["source_document_id"] = source_document_id
        return stats

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------
# MySQL 形态（正式）
# --------------------------------------------------------------------------


class MysqlRepository(M2Repository):
    """正式形态：MySQL 8，ON DUPLICATE KEY UPDATE。DDL 见 schema.sql §A。

    注意：本类**不会**创建或写入 ods_m1_* / ods_m4_*，只 SELECT ods_m1_*。
    """

    dialect = "mysql"

    def __init__(self, host: str, port: int, user: str, password: str, dbname: str):
        import pymysql

        self._pymysql = pymysql
        self._lock = threading.RLock()
        # autocommit=True 是必须的：只读路径（fetch_document / list_pending / stats）
        # 从不 commit，若用 autocommit=False 就会一直挂着一个长事务，MySQL 默认
        # REPEATABLE READ 下快照被冻在事务开始那一刻——实测会导致服务跑了一整天
        # 还在读昨天的数据（/m2/pending 永远报 0、fetch_document 取到陈旧输入）。
        # 写入路径自己用 conn.begin() 开显式事务，仍然是单事务原子提交。
        self._conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=dbname,
            charset="utf8mb4",
            autocommit=True,
        )

    def _cursor(self, dict_rows: bool = False):
        # M2 单文档归并要跑十几秒到几十秒，两次调用之间可能空闲数小时；
        # 先 ping 重连，避免 MySQL wait_timeout 断连后首次请求 500。
        self._conn.ping(reconnect=True)
        return self._conn.cursor(
            self._pymysql.cursors.DictCursor if dict_rows else self._pymysql.cursors.Cursor
        )

    # ---- schema --------------------------------------------------
    def bootstrap_schema(self) -> dict:
        statements = load_mysql_ddl()
        cursor = self._cursor()
        try:
            with self._lock:
                for stmt in statements:
                    cursor.execute(stmt)
                self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()
        tables = sum(1 for s in statements if re.match(r"(?is)^CREATE TABLE", s))
        views = sum(1 for s in statements if re.match(r"(?is)^CREATE OR REPLACE VIEW", s))
        return {"dialect": self.dialect, "statements": len(statements), "tables": tables, "views": views}

    # ---- 读侧 ----------------------------------------------------
    def list_documents(self) -> List[dict]:
        cursor = self._cursor(dict_rows=True)
        try:
            with self._lock:
                cursor.execute(
                    "SELECT source_document_id, file_name, meeting_date, mode, item_count "
                    "FROM ods_m1_source_documents ORDER BY source_document_id"
                )
                rows = cursor.fetchall()
        finally:
            cursor.close()
        return [
            {
                "source_document_id": r["source_document_id"],
                "file_name": r["file_name"],
                "meeting_date": _date_to_str(r["meeting_date"]),
                "mode": r["mode"],
                "item_count": int(r["item_count"]),
            }
            for r in rows
        ]

    def fetch_items(self, source_document_id: str) -> List[dict]:
        cursor = self._cursor(dict_rows=True)
        try:
            with self._lock:
                cursor.execute(
                    "SELECT item_json FROM ods_m1_meeting_items "
                    "WHERE source_document_id = %s ORDER BY item_seq",
                    (source_document_id,),
                )
                rows = cursor.fetchall()
        finally:
            cursor.close()
        return [_loads(row["item_json"]) for row in rows]

    def _fetch_all_items_grouped(self) -> tuple:
        """一次捞全量 M1 条目（2077 行量级）与已归并指纹，供增量判定。"""
        cursor = self._cursor(dict_rows=True)
        try:
            with self._lock:
                cursor.execute(
                    "SELECT source_document_id, item_json FROM ods_m1_meeting_items "
                    "ORDER BY source_document_id, item_seq"
                )
                rows = cursor.fetchall()
                cursor.execute(
                    "SELECT source_document_id, input_fingerprint FROM ods_m2_consolidated_documents"
                )
                known = cursor.fetchall()
        finally:
            cursor.close()
        grouped: Dict[str, List[dict]] = {}
        for row in rows:
            grouped.setdefault(row["source_document_id"], []).append(_loads(row["item_json"]))
        fingerprints = {r["source_document_id"]: r["input_fingerprint"] for r in known}
        return grouped, fingerprints

    def fetch_document(self, source_document_id: str) -> Optional[dict]:
        cursor = self._cursor(dict_rows=True)
        try:
            with self._lock:
                cursor.execute(
                    "SELECT source_document_id, file_name, meeting_date, mode, item_count "
                    "FROM ods_m1_source_documents WHERE source_document_id = %s",
                    (source_document_id,),
                )
                row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            return None
        return {
            "source_document_id": row["source_document_id"],
            "file_name": row["file_name"],
            "meeting_date": _date_to_str(row["meeting_date"]),
            "mode": row["mode"],
            "item_count": int(row["item_count"]),
            "items": self.fetch_items(source_document_id),
        }

    def list_pending(self, limit: Optional[int] = None) -> List[dict]:
        grouped, fingerprints = self._fetch_all_items_grouped()
        return _build_pending(self.list_documents(), grouped, fingerprints, limit)

    # ---- 写侧 ----------------------------------------------------
    def upsert_consolidation(
        self,
        doc_row: dict,
        items: List[dict],
        trace: List[dict],
        project_entities: List[dict],
        issues: List[dict],
    ) -> dict:
        now = utcnow()
        doc_id = doc_row["source_document_id"]
        rows = [m2_item_to_row(doc_id, idx, item, trace[idx]) for idx, item in enumerate(items)]

        placeholders = ", ".join(["%s"] * (len(_DOC_COLUMNS) + 2))
        doc_updates = ", ".join(
            "{}=VALUES({})".format(col, col) for col in _DOC_COLUMNS if col != "source_document_id"
        ) + ", updated_at=VALUES(updated_at)"
        doc_sql = (
            "INSERT INTO ods_m2_consolidated_documents ({}, ingested_at, updated_at) "
            "VALUES ({}) ON DUPLICATE KEY UPDATE {}".format(
                ", ".join(_DOC_COLUMNS), placeholders, doc_updates
            )
        )

        item_placeholders = ", ".join(["%s"] * (len(_ITEM_COLUMNS) + 2))
        item_updates = ", ".join(
            "{}=VALUES({})".format(col, col) for col in _ITEM_COLUMNS if col != "item_id"
        ) + ", updated_at=VALUES(updated_at)"
        item_sql = (
            "INSERT INTO ods_m2_meeting_items ({}, created_at, updated_at) "
            "VALUES ({}) ON DUPLICATE KEY UPDATE {}".format(
                ", ".join(_ITEM_COLUMNS), item_placeholders, item_updates
            )
        )

        entity_placeholders = ", ".join(["%s"] * (len(_ENTITY_COLUMNS) + 2))
        entity_sql = (
            "INSERT INTO ods_m2_project_entities ({}, created_at, updated_at) "
            "VALUES ({}) ON DUPLICATE KEY UPDATE canonical_name=VALUES(canonical_name), "
            "aliases_json=VALUES(aliases_json), source_names_json=VALUES(source_names_json), "
            "decisions_json=VALUES(decisions_json), updated_at=VALUES(updated_at)".format(
                ", ".join(_ENTITY_COLUMNS), entity_placeholders
            )
        )
        issue_placeholders = ", ".join(["%s"] * (len(_ISSUE_COLUMNS) + 1))
        issue_sql = "INSERT INTO ods_m2_validation_issues ({}, created_at) VALUES ({})".format(
            ", ".join(_ISSUE_COLUMNS), issue_placeholders
        )

        cursor = self._cursor()
        try:
            with self._lock:
                self._conn.begin()   # 显式事务：本文档全部写入要么全成要么全滚
                cursor.execute(
                    doc_sql, [doc_row[col] for col in _DOC_COLUMNS] + [now, now]
                )
                cursor.execute(
                    "DELETE FROM ods_m2_meeting_items "
                    "WHERE source_document_id = %s AND item_seq >= %s",
                    (doc_id, len(rows)),
                )
                for row in rows:
                    cursor.execute(item_sql, [row[col] for col in _ITEM_COLUMNS] + [now, now])
                cursor.execute(
                    "DELETE FROM ods_m2_project_entities WHERE source_document_id = %s", (doc_id,)
                )
                for entity in project_entities:
                    erow = entity_to_row(doc_id, entity)
                    cursor.execute(
                        entity_sql, [erow[col] for col in _ENTITY_COLUMNS] + [now, now]
                    )
                cursor.execute(
                    "DELETE FROM ods_m2_validation_issues WHERE source_document_id = %s", (doc_id,)
                )
                for issue in issues:
                    irow = issue_to_row(doc_id, issue)
                    cursor.execute(issue_sql, [irow[col] for col in _ISSUE_COLUMNS] + [now])
                self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()
        return {
            "source_document_id": doc_id,
            "item_count": len(rows),
            "project_entity_count": len(project_entities),
            "issue_count": len(issues),
        }

    def stats(self, source_document_id: str) -> dict:
        cursor = self._cursor(dict_rows=True)
        try:
            with self._lock:
                cursor.execute(
                    "SELECT * FROM ods_m2_consolidated_documents WHERE source_document_id = %s",
                    (source_document_id,),
                )
                doc = cursor.fetchone()
                cursor.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(merged), 0) AS m, "
                    "COALESCE(SUM(source_count), 0) AS s FROM ods_m2_meeting_items "
                    "WHERE source_document_id = %s",
                    (source_document_id,),
                )
                agg = cursor.fetchone()
                cursor.execute(
                    "SELECT item_type, COUNT(*) AS cnt FROM ods_m2_meeting_items "
                    "WHERE source_document_id = %s GROUP BY item_type",
                    (source_document_id,),
                )
                by_type = cursor.fetchall()
                cursor.execute(
                    "SELECT evidence_mode, COUNT(*) AS cnt FROM ods_m2_meeting_items "
                    "WHERE source_document_id = %s GROUP BY evidence_mode",
                    (source_document_id,),
                )
                by_mode = cursor.fetchall()
                cursor.execute(
                    "SELECT level, COUNT(*) AS cnt FROM ods_m2_validation_issues "
                    "WHERE source_document_id = %s GROUP BY level",
                    (source_document_id,),
                )
                by_level = cursor.fetchall()
                cursor.execute(
                    "SELECT COUNT(*) AS cnt FROM ods_m2_project_entities "
                    "WHERE source_document_id = %s",
                    (source_document_id,),
                )
                ent = cursor.fetchone()
        finally:
            cursor.close()
        counters = {
            "item_count": int(agg["n"] or 0),
            "merged_count": int(agg["m"] or 0),
            "source_item_count_covered": int(agg["s"] or 0),
            "by_item_type": {r["item_type"]: int(r["cnt"]) for r in by_type},
            "by_evidence_mode": {r["evidence_mode"]: int(r["cnt"]) for r in by_mode},
            "issues": dict(_EMPTY_COUNTERS["issues"]),
            "project_entity_count": int(ent["cnt"] or 0),
        }
        for r in by_level:
            counters["issues"][r["level"]] = int(r["cnt"])
        stats = _build_stats(_row_to_doc(doc), counters)
        stats["source_document_id"] = source_document_id
        return stats

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _loads(value: Any) -> dict:
    """MySQL JSON 列 pymysql 返回 str；SQLite TEXT 列同样是 str。两边都过一遍。"""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value) if isinstance(value, str) else value


def build_repository(dsn: str, base_dir: Optional[str] = None) -> M2Repository:
    """解析 M2_DSN 并构造仓储。"""
    parsed = urllib.parse.urlsplit(dsn)
    scheme = (parsed.scheme or "").lower()
    if scheme.startswith("sqlite"):
        import os

        path = parsed.path
        if not path.startswith("/"):
            path = os.path.join(base_dir or str(HERE), path)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        return SqliteRepository(path)
    if scheme.startswith("mysql"):
        return MysqlRepository(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=urllib.parse.unquote(parsed.username or ""),
            password=urllib.parse.unquote(parsed.password or ""),
            dbname=(parsed.path or "/").lstrip("/"),
        )
    raise ValueError("不支持的 DSN scheme：{}".format(scheme))
