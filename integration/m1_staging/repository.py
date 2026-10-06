# -*- coding: utf-8 -*-
"""M1 staging 仓储层：以 repository 模式隔离 MySQL / SQLite 方言。

- 正式形态走 MySQL 8（DSN 走环境变量 M1_STAGING_DSN，DDL 见 schema.sql）；
- 本机无可用 MySQL 实例时用 SQLite 适配层自测，业务语义完全一致：
  单事务 upsert、重复 ingest 行数不变、updated_at 刷新。

DSN 形态：
    mysql://user:YOUR_PASSWORD@host:port/dbname
    sqlite:////absolute/path/m1_staging.db
    sqlite:///relative/path/m1_staging.db   （相对 integration/ 目录）
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timezone
from typing import List, Optional

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
]

DOC_SQLITE_DDL = """
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

ITEM_SQLITE_DDL = """
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

ITEM_INDEX_SQLITE = [
    "CREATE INDEX IF NOT EXISTS idx_m1_project ON ods_m1_meeting_items (project)",
    "CREATE INDEX IF NOT EXISTS idx_m1_dept ON ods_m1_meeting_items (department)",
    "CREATE INDEX IF NOT EXISTS idx_m1_type ON ods_m1_meeting_items (item_type)",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def derive_item_id(source_document_id: str, idx: int) -> str:
    """稳定 ID：与 M3 图身份体系同构，便于未来图-表关联。"""
    digest = hashlib.sha1("{}#{}".format(source_document_id, idx).encode("utf-8")).hexdigest()
    return "item:m1:" + digest


def doc_id_from_path(path) -> str:
    """稳定文档身份：去掉 .items.json / .m1.json 后缀的文件名（如 2026-04-07）。"""
    from pathlib import Path

    path = Path(path)
    name = path.name
    for suffix in (".items.json", ".m1.json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def meeting_date_from_doc_id(source_document_id: str) -> Optional[str]:
    """会议日期兜底：本语料文件名约定即会议日期，source_document_id 形如
    YYYY-MM-DD 时确定性解析为会议日期；否则返回 None（不做任何猜测）。"""
    try:
        return datetime.strptime(source_document_id, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def item_to_row(source_document_id: str, idx: int, item: dict) -> dict:
    evidence = item["evidence"]
    return {
        "item_id": derive_item_id(source_document_id, idx),
        "source_document_id": source_document_id,
        "item_seq": idx,
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
    }


class StagingRepository:
    """方言无关的接口约定：upsert 单事务、幂等、统计只读。"""

    dialect = "abstract"

    def upsert_batch(
        self,
        source_document_id: str,
        file_name: str,
        meeting_date: Optional[str],
        mode: str,
        items: List[dict],
    ) -> dict:
        raise NotImplementedError

    def stats(self, source_document_id: str) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SqliteRepository(StagingRepository):
    """自测/单机适配层。进程内单连接 + 锁，语义与 MySQL 版一致。"""

    dialect = "sqlite"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.execute(DOC_SQLITE_DDL)
            self._conn.execute(ITEM_SQLITE_DDL)
            for stmt in ITEM_INDEX_SQLITE:
                self._conn.execute(stmt)
            self._conn.commit()

    def upsert_batch(
        self,
        source_document_id: str,
        file_name: str,
        meeting_date: Optional[str],
        mode: str,
        items: List[dict],
    ) -> dict:
        now = utcnow()
        rows = [item_to_row(source_document_id, idx, item) for idx, item in enumerate(items)]
        placeholders = ", ".join("?" for _ in _ITEM_COLUMNS)
        updates = ", ".join(
            "{}=excluded.{}".format(col, col) for col in _ITEM_COLUMNS if col != "item_id"
        ) + ", updated_at=excluded.updated_at"
        item_sql = (
            "INSERT INTO ods_m1_meeting_items ({}, created_at, updated_at) "
            "VALUES ({}, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET {}".format(
                ", ".join(_ITEM_COLUMNS), placeholders, updates
            )
        )
        doc_sql = (
            "INSERT INTO ods_m1_source_documents "
            "(source_document_id, file_name, meeting_date, mode, item_count, ingested_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_document_id) DO UPDATE SET "
            "file_name=excluded.file_name, meeting_date=excluded.meeting_date, "
            "mode=excluded.mode, item_count=excluded.item_count, updated_at=excluded.updated_at"
        )
        with self._lock:
            try:
                self._conn.execute(
                    doc_sql,
                    (source_document_id, file_name, meeting_date, mode, len(rows), now, now),
                )
                # 重灌时条目可能变少（LLM 有方差）：不清尾行就会把上一轮的
                # 残留当成当前数据，造成 doc.item_count 与 items 实际行数不一致。
                self._conn.execute(
                    "DELETE FROM ods_m1_meeting_items "
                    "WHERE source_document_id = ? AND item_seq >= ?",
                    (source_document_id, len(rows)),
                )
                for row in rows:
                    values = [row[col] for col in _ITEM_COLUMNS] + [now, now]
                    self._conn.execute(item_sql, values)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"item_count": len(rows)}

    def stats(self, source_document_id: str) -> dict:
        with self._lock:
            doc = self._conn.execute(
                "SELECT file_name, meeting_date, mode, item_count, ingested_at, updated_at "
                "FROM ods_m1_source_documents WHERE source_document_id = ?",
                (source_document_id,),
            ).fetchone()
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(exact_match), 0) AS exact "
                "FROM ods_m1_meeting_items WHERE source_document_id = ?",
                (source_document_id,),
            ).fetchone()
        count, exact = int(row["cnt"]), int(row["exact"])
        return {
            "source_document_id": source_document_id,
            "document": dict(doc) if doc else None,
            "item_count": count,
            "exact_match_count": exact,
            "exact_match_rate": round(exact / count, 4) if count else 0.0,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class MysqlRepository(StagingRepository):
    """正式形态：MySQL 8，ON DUPLICATE KEY UPDATE。DDL 见 schema.sql。"""

    dialect = "mysql"

    def __init__(self, host: str, port: int, user: str, password: str, dbname: str):
        import pymysql

        self._pymysql = pymysql
        self._lock = threading.Lock()
        # autocommit=True：只读路径（stats）从不 commit，若用 autocommit=False 会一直
        # 挂着长事务，MySQL 默认 REPEATABLE READ 下快照被冻在事务开始那一刻，
        # /m1/stats 会长期返回陈旧计数。写入路径用 conn.begin() 开显式事务。
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
        """取 cursor 前先 ping 重连。

        本服务持一条长连接，而 MySQL wait_timeout=28800（8 小时）；隔夜里没人调
        就会被服务端单方面关掉，下次请求直接 `pymysql.err.InterfaceError: (0, '')`
        并给客户端返回 500。ingest 路径也一样会挂，不只是 stats。
        """
        self._conn.ping(reconnect=True)
        return self._conn.cursor(
            self._pymysql.cursors.DictCursor if dict_rows else self._pymysql.cursors.Cursor
        )

    def upsert_batch(
        self,
        source_document_id: str,
        file_name: str,
        meeting_date: Optional[str],
        mode: str,
        items: List[dict],
    ) -> dict:
        now = utcnow()
        rows = [item_to_row(source_document_id, idx, item) for idx, item in enumerate(items)]
        placeholders = ", ".join(["%s"] * (len(_ITEM_COLUMNS) + 2))
        updates = ", ".join(
            "{}=VALUES({})".format(col, col) for col in _ITEM_COLUMNS if col != "item_id"
        ) + ", updated_at=VALUES(updated_at)"
        item_sql = (
            "INSERT INTO ods_m1_meeting_items ({}, created_at, updated_at) "
            "VALUES ({}) ON DUPLICATE KEY UPDATE {}".format(
                ", ".join(_ITEM_COLUMNS), placeholders, updates
            )
        )
        doc_sql = (
            "INSERT INTO ods_m1_source_documents "
            "(source_document_id, file_name, meeting_date, mode, item_count, ingested_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE file_name=VALUES(file_name), "
            "meeting_date=VALUES(meeting_date), mode=VALUES(mode), "
            "item_count=VALUES(item_count), updated_at=VALUES(updated_at)"
        )
        cursor = self._cursor()
        try:
            with self._lock:
                self._conn.begin()   # 显式事务：本文档全部写入要么全成要么全滚
                cursor.execute(
                    doc_sql,
                    (source_document_id, file_name, meeting_date, mode, len(rows), now, now),
                )
                # 重灌时条目可能变少（LLM 有方差）：不清尾行就会把上一轮的
                # 残留当成当前数据，造成 doc.item_count 与 items 实际行数不一致，
                # 下游输入指纹及中台数据服务 API 都会读取到错误来源。
                cursor.execute(
                    "DELETE FROM ods_m1_meeting_items "
                    "WHERE source_document_id = %s AND item_seq >= %s",
                    (source_document_id, len(rows)),
                )
                for row in rows:
                    values = [row[col] for col in _ITEM_COLUMNS] + [now, now]
                    cursor.execute(item_sql, values)
                self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()
        return {"item_count": len(rows)}

    def stats(self, source_document_id: str) -> dict:
        cursor = self._cursor(dict_rows=True)
        try:
            with self._lock:
                cursor.execute(
                    "SELECT file_name, meeting_date, mode, item_count, ingested_at, updated_at "
                    "FROM ods_m1_source_documents WHERE source_document_id = %s",
                    (source_document_id,),
                )
                doc = cursor.fetchone()
                cursor.execute(
                    "SELECT COUNT(*) AS cnt, COALESCE(SUM(exact_match), 0) AS exact "
                    "FROM ods_m1_meeting_items WHERE source_document_id = %s",
                    (source_document_id,),
                )
                row = cursor.fetchone()
        finally:
            cursor.close()
        count, exact = int(row["cnt"]), int(row["exact"])
        if doc:
            doc = {k: str(v) if isinstance(v, datetime) else v for k, v in doc.items()}
        return {
            "source_document_id": source_document_id,
            "document": doc,
            "item_count": count,
            "exact_match_count": exact,
            "exact_match_rate": round(exact / count, 4) if count else 0.0,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def build_repository(dsn: str, base_dir: str) -> StagingRepository:
    """解析 M1_STAGING_DSN 并构造仓储。"""
    parsed = urllib.parse.urlsplit(dsn)
    scheme = parsed.scheme.lower()
    if scheme.startswith("sqlite"):
        import os

        path = urllib.parse.unquote(parsed.path)
        if (os.name == "nt" and len(path) > 3 and path[0] == "/"
                and path[1].isalpha() and path[2] == ":"):
            path = path[1:]
        if not os.path.isabs(path):
            path = os.path.join(base_dir, path)
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
