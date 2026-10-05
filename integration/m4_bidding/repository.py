# -*- coding: utf-8 -*-
"""M4 bidding 仓储层：以 repository 模式隔离 MySQL / SQLite 方言。

表结构与关联方式与 m1_staging/repository.py 严格同构（见 schema.sql）：
- 正式形态走 MySQL 8（DSN 走环境变量 M4_BIDDING_DSN，DDL 见 schema.sql）；
- 本机无可用 MySQL 实例时用 SQLite 适配层自测，业务语义完全一致：
  单事务 upsert、重复 ingest 行数不变、updated_at 刷新。

DSN 形态（与 m1_staging 一致）：
    mysql://user:YOUR_PASSWORD@host:port/dbname
    sqlite:////absolute/path/m4_bidding.db
    sqlite:///relative/path/m4_bidding.db   （相对 integration/m4_bidding/ 目录）
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, List, Optional

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
    # m4 追加的招投标注记三列
    "bidding_category",
    "bidding_reason",
    "origin_item_id",
]

DOC_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS ods_m4_bidding_documents (
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
CREATE TABLE IF NOT EXISTS ods_m4_bidding_items (
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
  bidding_category   TEXT NOT NULL,
  bidding_reason     TEXT NOT NULL,
  origin_item_id     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (source_document_id, item_seq)
)
"""

ITEM_INDEX_SQLITE = [
    "CREATE INDEX IF NOT EXISTS idx_m4_project ON ods_m4_bidding_items (project)",
    "CREATE INDEX IF NOT EXISTS idx_m4_dept ON ods_m4_bidding_items (department)",
    "CREATE INDEX IF NOT EXISTS idx_m4_category ON ods_m4_bidding_items (bidding_category)",
    "CREATE INDEX IF NOT EXISTS idx_m4_origin ON ods_m4_bidding_items (origin_item_id)",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def derive_item_id(source_document_id: str, idx: int) -> str:
    """m4 稳定 ID：与 m1 derive_item_id 同法，前缀换为 item:m4:。"""
    digest = hashlib.sha1("{}#{}".format(source_document_id, idx).encode("utf-8")).hexdigest()
    return "item:m4:" + digest


def derive_origin_item_id(source_document_id: str, idx: int) -> str:
    """回链 m1 原条目：与 m1_staging.repository.derive_item_id 完全一致。"""
    digest = hashlib.sha1("{}#{}".format(source_document_id, idx).encode("utf-8")).hexdigest()
    return "item:m1:" + digest


def doc_id_from_path(path) -> str:
    """稳定文档身份：与 m1_staging 同法——去掉 .items.json / .m1.json / .bidding.json
    / .filtered.json 后缀的文件名（如 2026-04-07）。"""
    from pathlib import Path

    path = Path(path)
    name = path.name
    for suffix in (".items.json", ".m1.json", ".bidding.json", ".filtered.json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def meeting_date_from_doc_id(source_document_id: str) -> Optional[str]:
    """会议日期兜底：doc id 形如 YYYY-MM-DD 时确定性解析，否则 None（不猜测）。"""
    try:
        return datetime.strptime(source_document_id, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


# 日期前缀：兼容 2026-08-24 / 2026.8.24 / 2026/8/24，且允许后面跟中文文件名
_DATE_PREFIX = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")


def date_from_doc_id(source_document_id: Optional[str]) -> Optional[str]:
    """从 doc id / 文件名里确定性提取规范日期 ``YYYY-MM-DD``；提不出来返回 None。

    与 ``meeting_date_from_doc_id`` 的区别：后者只认严格的 YYYY-MM-DD 整串，
    本函数还能从 ``2026.8.24信息公司周例会工作安排备忘录.pdf`` 这种文件名形态、
    以及 m1_service 返回的 ``doc:<文件名>`` 形态里取出日期。月份/日期非法
    （如 2026-13-45）由 datetime 构造报错并返回 None，不猜。
    """
    if not source_document_id:
        return None
    text = source_document_id
    if text.startswith("doc:"):          # m1_service /m1/extract 返回的临时身份
        text = text[len("doc:"):]
    match = _DATE_PREFIX.search(text)
    if not match:
        return None
    try:
        year, month, day = (int(part) for part in match.groups())
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def resolve_doc_id(source_document_id, lookup_exact, lookup_by_date) -> str:
    """把传入的 doc id 归一到 `ods_m1_source_documents` 里**已登记**的那一个。

    为何要向 M1 口径归一（而不是都归到 YYYY-MM-DD）：M4 消费的是 M1 的输出，
    `origin_item_id` = "item:m1:"+sha1(doc_id#idx) 必须用 M1 实际入库时用的 doc_id
    才能 JOIN 回 `ods_m1_meeting_items`。实测踩过：8.24 那份 M1 登记的是文件名
    形态 `2026.8.24信息公司周例会工作安排备忘录`，而 M4 当年用了 `2026-08-24`，
    两边血缘完全对不上。

    解析不到就**原样返回**：M4 允许在 M1 入库之前独立跑（CLI 与单测就是这种），
    不能因为查不到参照物就报错。同一天对应多份文档属歧义，也不猜。
    """
    if not source_document_id:
        return source_document_id
    exact = lookup_exact(source_document_id)
    if exact:
        return exact
    date = date_from_doc_id(source_document_id)
    if not date:
        return source_document_id
    if date != source_document_id:
        hit = lookup_exact(date)
        if hit:
            return hit
    hits = lookup_by_date(date)
    if len(hits) == 1:
        return hits[0]
    return source_document_id


def bidding_item_to_row(source_document_id: str, idx: int, item: dict, annotation: dict) -> dict:
    """m1 九字段 item + 招投标注记 → 表行。item_seq 沿用 m1 原始下标保持可回溯。"""
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
        "bidding_category": annotation["category"],
        "bidding_reason": annotation["reason"],
        "origin_item_id": derive_origin_item_id(source_document_id, idx),
    }


class BiddingRepository:
    """方言无关的接口约定：upsert 单事务、幂等、统计只读。"""

    dialect = "abstract"

    def upsert_bidding(
        self,
        source_document_id: str,
        file_name: str,
        meeting_date: Optional[str],
        mode: str,
        items: List[dict],
        annotations: Dict[int, dict],
    ) -> dict:
        """items 为该文档全部 m1 条目（按原下标），annotations 为 idx → 注记
        （仅含 LLM 判为招投标的条目）。幂等：重复调用行数不变。"""
        raise NotImplementedError

    def stats(self, source_document_id: str) -> dict:
        raise NotImplementedError

    def resolve_source_document_id(self, source_document_id: str) -> str:
        """归一 doc id；缺省不归一（无参照表时保持原样）。"""
        return source_document_id

    def close(self) -> None:
        pass


class SqliteBiddingRepository(BiddingRepository):
    """自测/单机适配层。进程内单连接 + 锁，语义与 MySQL 版一致。"""

    dialect = "sqlite"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.execute(DOC_SQLITE_DDL)
            self._conn.execute(ITEM_SQLITE_DDL)
            for stmt in ITEM_INDEX_SQLITE:
                self._conn.execute(stmt)
            self._conn.commit()

    def _upsert_doc(self, cursor, source_document_id, file_name, meeting_date, mode, total, now):
        cursor.execute(
            "INSERT INTO ods_m4_bidding_documents "
            "(source_document_id, file_name, meeting_date, mode, item_count, ingested_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_document_id) DO UPDATE SET "
            "file_name=excluded.file_name, meeting_date=excluded.meeting_date, "
            "mode=excluded.mode, item_count=excluded.item_count, updated_at=excluded.updated_at",
            (source_document_id, file_name, meeting_date, mode, total, now, now),
        )

    def upsert_bidding(
        self,
        source_document_id: str,
        file_name: str,
        meeting_date: Optional[str],
        mode: str,
        items: List[dict],
        annotations: Dict[int, dict],
    ) -> dict:
        now = utcnow()
        rows = [
            bidding_item_to_row(source_document_id, idx, items[idx], annotations[idx])
            for idx in sorted(annotations)
        ]
        placeholders = ", ".join("?" for _ in _ITEM_COLUMNS)
        updates = ", ".join(
            "{}=excluded.{}".format(col, col) for col in _ITEM_COLUMNS if col != "item_id"
        ) + ", updated_at=excluded.updated_at"
        item_sql = (
            "INSERT INTO ods_m4_bidding_items ({}, created_at, updated_at) "
            "VALUES ({}, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET {}".format(
                ", ".join(_ITEM_COLUMNS), placeholders, updates
            )
        )
        with self._lock:
            try:
                cursor = self._conn.cursor()
                self._upsert_doc(
                    cursor, source_document_id, file_name, meeting_date, mode, len(items), now
                )
                # 招投标条目是稀疏子集，本轮判为招投标的 item_seq 与上轮不一定重合；
                # 只 upsert 会把上轮的残留行留下来（且计数还可能巧合相等而看不出来），
                # 所以按文档先删后插，保证表里恰好是本轮的判定结果。
                cursor.execute(
                    "DELETE FROM ods_m4_bidding_items WHERE source_document_id = ?",
                    (source_document_id,),
                )
                for row in rows:
                    values = [row[col] for col in _ITEM_COLUMNS] + [now, now]
                    cursor.execute(item_sql, values)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"bidding_count": len(rows), "item_count": len(items)}

    def stats(self, source_document_id: str) -> dict:
        with self._lock:
            doc = self._conn.execute(
                "SELECT file_name, meeting_date, mode, item_count, ingested_at, updated_at "
                "FROM ods_m4_bidding_documents WHERE source_document_id = ?",
                (source_document_id,),
            ).fetchone()
            by_category = self._conn.execute(
                "SELECT bidding_category, COUNT(*) AS cnt FROM ods_m4_bidding_items "
                "WHERE source_document_id = ? GROUP BY bidding_category",
                (source_document_id,),
            ).fetchall()
            total = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM ods_m4_bidding_items WHERE source_document_id = ?",
                (source_document_id,),
            ).fetchone()
        return {
            "source_document_id": source_document_id,
            "document": dict(doc) if doc else None,
            "bidding_count": int(total["cnt"]),
            "by_category": {r["bidding_category"]: int(r["cnt"]) for r in by_category},
        }

    # ---- doc id 归一（向 ods_m1_source_documents 已登记的口径对齐）----
    def _m1_lookup_exact(self, doc_id: str) -> Optional[str]:
        try:
            row = self._conn.execute(
                "SELECT source_document_id FROM ods_m1_source_documents "
                "WHERE source_document_id = ?",
                (doc_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # 自测库里没有 ods_m1_* 表（m4 的 SQLite DDL 只建 ods_m4_*）：
            # M4 可以脱离 M1 独立跑，此时不归一。
            return None
        return row["source_document_id"] if row else None

    def _m1_lookup_by_date(self, meeting_date: str) -> List[str]:
        try:
            rows = self._conn.execute(
                "SELECT source_document_id FROM ods_m1_source_documents "
                "WHERE meeting_date = ?",
                (meeting_date,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [row["source_document_id"] for row in rows]

    def resolve_source_document_id(self, source_document_id: str) -> str:
        with self._lock:
            return resolve_doc_id(
                source_document_id, self._m1_lookup_exact, self._m1_lookup_by_date
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class MysqlBiddingRepository(BiddingRepository):
    """正式形态：MySQL 8，ON DUPLICATE KEY UPDATE。DDL 见 schema.sql §A。"""

    dialect = "mysql"

    def __init__(self, host: str, port: int, user: str, password: str, dbname: str):
        import pymysql

        self._pymysql = pymysql
        self._lock = threading.Lock()
        # autocommit=True：只读路径（stats）从不 commit，若用 autocommit=False 会一直
        # 挂着长事务，MySQL 默认 REPEATABLE READ 下快照被冻在事务开始那一刻，
        # /m4/stats 会长期返回陈旧计数。写入路径用 conn.begin() 开显式事务。
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
        并给客户端返回 500。split 入库路径也一样会挂，不只是 stats。
        """
        self._conn.ping(reconnect=True)
        return self._conn.cursor(
            self._pymysql.cursors.DictCursor if dict_rows else self._pymysql.cursors.Cursor
        )

    def upsert_bidding(
        self,
        source_document_id: str,
        file_name: str,
        meeting_date: Optional[str],
        mode: str,
        items: List[dict],
        annotations: Dict[int, dict],
    ) -> dict:
        now = utcnow()
        rows = [
            bidding_item_to_row(source_document_id, idx, items[idx], annotations[idx])
            for idx in sorted(annotations)
        ]
        placeholders = ", ".join(["%s"] * (len(_ITEM_COLUMNS) + 2))
        updates = ", ".join(
            "{}=VALUES({})".format(col, col) for col in _ITEM_COLUMNS if col != "item_id"
        ) + ", updated_at=VALUES(updated_at)"
        item_sql = (
            "INSERT INTO ods_m4_bidding_items ({}, created_at, updated_at) "
            "VALUES ({}) ON DUPLICATE KEY UPDATE {}".format(
                ", ".join(_ITEM_COLUMNS), placeholders, updates
            )
        )
        doc_sql = (
            "INSERT INTO ods_m4_bidding_documents "
            "(source_document_id, file_name, meeting_date, mode, item_count, ingested_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE file_name=VALUES(file_name), "
            "meeting_date=VALUES(meeting_date), mode=VALUES(mode), "
            "item_count=VALUES(item_count), updated_at=VALUES(updated_at)"
        )
        cursor = self._cursor()
        try:
            with self._lock:
                self._conn.begin()   # 显式事务：先删后插必须原子，否则崩溃会丢整份判定
                cursor.execute(
                    doc_sql,
                    (source_document_id, file_name, meeting_date, mode, len(items), now, now),
                )
                # 招投标条目是稀疏子集，本轮判为招投标的 item_seq 与上轮不一定重合；
                # 只 upsert 会把上轮的残留行留下来（且计数还可能巧合相等而看不出来），
                # 所以按文档先删后插，保证表里恰好是本轮的判定结果。
                cursor.execute(
                    "DELETE FROM ods_m4_bidding_items WHERE source_document_id = %s",
                    (source_document_id,),
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
        return {"bidding_count": len(rows), "item_count": len(items)}

    def stats(self, source_document_id: str) -> dict:
        cursor = self._cursor(dict_rows=True)
        try:
            with self._lock:
                cursor.execute(
                    "SELECT file_name, meeting_date, mode, item_count, ingested_at, updated_at "
                    "FROM ods_m4_bidding_documents WHERE source_document_id = %s",
                    (source_document_id,),
                )
                doc = cursor.fetchone()
                cursor.execute(
                    "SELECT bidding_category, COUNT(*) AS cnt FROM ods_m4_bidding_items "
                    "WHERE source_document_id = %s GROUP BY bidding_category",
                    (source_document_id,),
                )
                by_category = cursor.fetchall()
                cursor.execute(
                    "SELECT COUNT(*) AS cnt FROM ods_m4_bidding_items WHERE source_document_id = %s",
                    (source_document_id,),
                )
                total = cursor.fetchone()
        finally:
            cursor.close()
        if doc:
            doc = {k: str(v) if isinstance(v, datetime) else v for k, v in doc.items()}
        return {
            "source_document_id": source_document_id,
            "document": doc,
            "bidding_count": int(total["cnt"]),
            "by_category": {r["bidding_category"]: int(r["cnt"]) for r in by_category},
        }

    # ---- doc id 归一（向 ods_m1_source_documents 已登记的口径对齐）----
    # 对 ods_m1_* **只 SELECT**，红线不变。
    def _m1_lookup_exact(self, cursor, doc_id: str) -> Optional[str]:
        try:
            cursor.execute(
                "SELECT source_document_id FROM ods_m1_source_documents "
                "WHERE source_document_id = %s",
                (doc_id,),
            )
            row = cursor.fetchone()
        except self._pymysql.err.MySQLError:
            # 表不存在（1146）或权限不足：M4 可脱离 M1 独立跑，此时不归一
            return None
        return row["source_document_id"] if row else None

    def _m1_lookup_by_date(self, cursor, meeting_date: str) -> List[str]:
        try:
            cursor.execute(
                "SELECT source_document_id FROM ods_m1_source_documents "
                "WHERE meeting_date = %s",
                (meeting_date,),
            )
            rows = cursor.fetchall()
        except self._pymysql.err.MySQLError:
            return []
        return [row["source_document_id"] for row in rows]

    def resolve_source_document_id(self, source_document_id: str) -> str:
        if not source_document_id:
            return source_document_id
        cursor = self._cursor(dict_rows=True)
        try:
            with self._lock:
                return resolve_doc_id(
                    source_document_id,
                    lambda doc_id: self._m1_lookup_exact(cursor, doc_id),
                    lambda meeting_date: self._m1_lookup_by_date(cursor, meeting_date),
                )
        finally:
            cursor.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def build_repository(dsn: str, base_dir: str) -> BiddingRepository:
    """解析 M4_BIDDING_DSN 并构造仓储。"""
    parsed = urllib.parse.urlsplit(dsn)
    scheme = parsed.scheme.lower()
    if scheme.startswith("sqlite"):
        import os

        path = parsed.path
        if not path.startswith("/"):
            path = os.path.join(base_dir, path)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        return SqliteBiddingRepository(path)
    if scheme.startswith("mysql"):
        return MysqlBiddingRepository(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=urllib.parse.unquote(parsed.username or ""),
            password=urllib.parse.unquote(parsed.password or ""),
            dbname=(parsed.path or "/").lstrip("/"),
        )
    raise ValueError("不支持的 DSN scheme：{}".format(scheme))
