"""M1.5 的项目主表、别名表与安全写入。"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_aliases (
    alias_normalized TEXT PRIMARY KEY,
    alias_name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_aliases_project ON project_aliases(project_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value: str) -> str:
    return re.sub(r"[\s，。；、：:（）()《》“”\"'_\-]+", "", value).lower()


class ProjectCatalog:
    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def list_for_model(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.project_id, p.canonical_name, a.alias_name
                FROM projects AS p
                LEFT JOIN project_aliases AS a ON a.project_id = p.project_id
                ORDER BY p.canonical_name, a.alias_name
                """
            ).fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            item = result.setdefault(
                row["project_id"],
                {
                    "project_id": row["project_id"],
                    "canonical_name": row["canonical_name"],
                    "aliases": [],
                },
            )
            if row["alias_name"]:
                item["aliases"].append(row["alias_name"])
        return list(result.values())

    def resolve_existing(self, project_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT project_id, canonical_name FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def resolve_source_name(self, source_name: str) -> dict | None:
        """Resolve a known project spelling without invoking the LLM."""
        source_key = normalize(source_name)
        if not source_key:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT p.project_id, p.canonical_name
                FROM project_aliases AS a
                JOIN projects AS p ON p.project_id = a.project_id
                WHERE a.alias_normalized = ?
                LIMIT 1
                """,
                (source_key,),
            ).fetchone()
        return dict(row) if row else None

    def create_or_add_alias(self, canonical_name: str, source_name: str) -> dict:
        canonical_name = canonical_name.strip()
        source_name = source_name.strip()
        if not canonical_name or not source_name:
            raise ValueError("项目主名和原始称谓不能为空")
        canonical_key = normalize(canonical_name)
        source_key = normalize(source_name)
        if not canonical_key or not source_key:
            raise ValueError("项目主名或原始称谓无可用字符")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT p.project_id, p.canonical_name
                FROM projects AS p
                LEFT JOIN project_aliases AS a ON a.project_id = p.project_id
                WHERE p.canonical_name = ? OR a.alias_normalized IN (?, ?)
                LIMIT 1
                """,
                (canonical_name, canonical_key, source_key),
            ).fetchone()
            now = _now()
            if row is None:
                project_id = "P" + uuid.uuid4().hex[:10].upper()
                resolved_name = canonical_name
                connection.execute(
                    """
                    INSERT INTO projects (project_id, canonical_name, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (project_id, resolved_name, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO project_aliases
                    (alias_normalized, alias_name, project_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (canonical_key, canonical_name, project_id, now),
                )
            else:
                project_id = row["project_id"]
                resolved_name = row["canonical_name"]
            conflict = connection.execute(
                "SELECT project_id FROM project_aliases WHERE alias_normalized = ?",
                (source_key,),
            ).fetchone()
            if conflict is not None and conflict["project_id"] != project_id:
                raise ValueError("原始项目称谓已关联到另一主项目，需人工复核")
            connection.execute(
                """
                INSERT OR IGNORE INTO project_aliases
                (alias_normalized, alias_name, project_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (source_key, source_name, project_id, now),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE project_id = ?",
                (now, project_id),
            )
            connection.commit()
        return {"project_id": project_id, "canonical_name": resolved_name}
