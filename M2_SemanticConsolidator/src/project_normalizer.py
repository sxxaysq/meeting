# -*- coding: utf-8 -*-
"""项目主表与 alias 存储。

沿用 ``M1_5_ProjectNormalizer`` 的 projects / project_aliases 两表思路，
但重新适配 M2：

* 只有**可靠的 SAME_ENTITY** 才写入永久 alias，UNCERTAIN 一律不落库；
* alias 命中是精确匹配（归一化后），不做模糊匹配——模糊只用于召回候选；
* 支持内存模式（``path=None``），单测与评估不需要落盘。
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

from text_utils import normalize_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    entity_id      TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS project_aliases (
    alias_key   TEXT PRIMARY KEY,
    alias       TEXT NOT NULL,
    entity_id   TEXT NOT NULL REFERENCES projects(entity_id),
    source      TEXT NOT NULL DEFAULT 'exact',
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_alias_entity ON project_aliases(entity_id);
"""


class ProjectCatalog:
    """项目主表 + 别名表。``path=None`` 时全在内存里。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.conn = sqlite3.connect(path or ":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 查询 -----------------------------------------------------
    def lookup(self, name: str) -> Optional[Dict[str, str]]:
        key = normalize_name(name)
        if not key:
            return None
        row = self.conn.execute(
            "SELECT p.entity_id, p.canonical_name FROM project_aliases a "
            "JOIN projects p ON p.entity_id = a.entity_id WHERE a.alias_key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row else None

    def all_projects(self) -> List[Dict[str, object]]:
        rows = self.conn.execute(
            "SELECT entity_id, canonical_name FROM projects ORDER BY entity_id"
        ).fetchall()
        result = []
        for row in rows:
            aliases = [
                r["alias"]
                for r in self.conn.execute(
                    "SELECT alias FROM project_aliases WHERE entity_id = ? ORDER BY alias",
                    (row["entity_id"],),
                ).fetchall()
            ]
            result.append(
                {
                    "entity_id": row["entity_id"],
                    "canonical_name": row["canonical_name"],
                    "aliases": aliases,
                }
            )
        return result

    def next_entity_id(self) -> str:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        return "P{:04d}".format(int(row["n"]) + 1)

    # ---- 写入 -----------------------------------------------------
    def ensure_project(self, canonical_name: str) -> Dict[str, str]:
        canonical_name = canonical_name.strip()
        if not canonical_name:
            raise ValueError("canonical_name 不能为空")
        row = self.conn.execute(
            "SELECT entity_id, canonical_name FROM projects WHERE canonical_name = ?",
            (canonical_name,),
        ).fetchone()
        if row:
            return dict(row)
        entity_id = self.next_entity_id()
        self.conn.execute(
            "INSERT INTO projects(entity_id, canonical_name) VALUES(?, ?)",
            (entity_id, canonical_name),
        )
        self.add_alias(entity_id, canonical_name, source="canonical")
        self.conn.commit()
        return {"entity_id": entity_id, "canonical_name": canonical_name}

    def add_alias(
        self, entity_id: str, alias: str, source: str = "llm", reason: str = ""
    ) -> None:
        """写别名。已指向别的实体时**不覆盖**，冲突留给 REVIEW 处理。"""
        key = normalize_name(alias)
        if not key:
            return
        existing = self.conn.execute(
            "SELECT entity_id FROM project_aliases WHERE alias_key = ?", (key,)
        ).fetchone()
        if existing:
            return
        self.conn.execute(
            "INSERT INTO project_aliases(alias_key, alias, entity_id, source, reason) "
            "VALUES(?, ?, ?, ?, ?)",
            (key, alias.strip(), entity_id, source, reason),
        )
        self.conn.commit()

    def alias_conflict(self, alias: str, entity_id: str) -> Optional[str]:
        """别名已经指向另一个实体时返回那个实体，供质量门报冲突。"""
        row = self.conn.execute(
            "SELECT entity_id FROM project_aliases WHERE alias_key = ?",
            (normalize_name(alias),),
        ).fetchone()
        if row and row["entity_id"] != entity_id:
            return str(row["entity_id"])
        return None

    def close(self) -> None:
        self.conn.close()
