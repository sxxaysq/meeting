"""SQLite repository for tasks, history, idempotency, and department data."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def decode_task(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    task = dict(row)
    task["assignees"] = _loads(task.pop("assignees_json", "[]"), [])
    return task


TASK_SELECT = 'SELECT t.* FROM tasks t'


class TaskRepository:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
        with self.connect() as connection:
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            if 'source_project' not in {r['name'] for r in connection.execute('PRAGMA table_info(tasks)')}:
                connection.execute('ALTER TABLE tasks ADD COLUMN source_project TEXT')
                connection.execute('UPDATE tasks SET source_project=project')
            connection.commit()

    def upsert_department(
        self,
        department_id: str,
        name: str,
        route: str,
        aliases: list[str] | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO departments (
                    department_id, name, route, aliases_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(department_id) DO UPDATE SET
                    name = excluded.name,
                    route = excluded.route,
                    aliases_json = excluded.aliases_json,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    department_id,
                    name.strip(),
                    route.strip(),
                    json.dumps(aliases or [], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.commit()

    def list_departments(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM departments WHERE active = 1 ORDER BY name"
            ).fetchall()
        result = []
        for row in rows:
            department = dict(row)
            department["aliases"] = _loads(
                department.pop("aliases_json", "[]"), []
            )
            result.append(department)
        return result

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                TASK_SELECT+" WHERE t.task_id = ?", (task_id,)
            ).fetchone()
        return decode_task(row) if row is not None else None

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                TASK_SELECT+" ORDER BY t.updated_at DESC, t.task_id"
            ).fetchall()
        return [decode_task(row) for row in rows]

    def retrieval_tasks(self, recent_event_limit: int = 5) -> list[dict[str, Any]]:
        tasks = self.list_tasks()
        with self.connect() as connection:
            for task in tasks:
                rows = connection.execute(
                    """
                    SELECT e.event_id, e.event_type, e.content, e.created_at,
                           e.source_document_id,e.source_item_id,
                           (SELECT evidence_json FROM task_source_links s WHERE s.event_id=e.event_id LIMIT 1) AS evidence_json
                    FROM task_events e
                    WHERE e.task_id = ?
                    ORDER BY e.rowid DESC
                    LIMIT ?
                    """,
                    (task["task_id"], recent_event_limit),
                ).fetchall()
                task["recent_events"] = [dict(row) for row in rows]
                for event in task['recent_events']:
                    event['evidence']=_loads(event.pop('evidence_json'),{})
                task['last_event_id'] = rows[0]['event_id'] if rows else None
        return tasks

    def get_processing_record(
        self,
        source_document_id: str,
        source_item_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM processing_records
                WHERE source_document_id = ? AND source_item_id = ?
                """,
                (source_document_id, source_item_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = _loads(result.pop("result_json"), {})
        return result

    def list_events(self, task_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM task_events"
        values: tuple[Any, ...] = ()
        if task_id:
            sql += " WHERE task_id = ?"
            values = (task_id,)
        sql += " ORDER BY created_at, event_id"
        with self.connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [dict(row) for row in rows]

    def list_dispatches(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dispatch_queue ORDER BY created_at, dispatch_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_reviews(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lifecycle_reviews ORDER BY created_at, review_id"
            ).fetchall()
        result = []
        for row in rows:
            review = dict(row)
            review["item"] = _loads(review.pop("item_json"), {})
            review["candidates"] = _loads(review.pop("candidates_json"), [])
            result.append(review)
        return result

    def add_historical_task(self, task: dict[str, Any]) -> None:
        """Import a trusted historical task; this never runs on LLM output."""
        required = {
            "task_id",
            "item_type",
            "department_id",
            "department",
            "title",
            "description",
            "status",
        }
        missing = required - set(task)
        if missing:
            raise ValueError(f"历史任务缺少字段：{sorted(missing)}")
        now = task.get("created_at") or utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, item_type, project_entity_id, project, source_project,
                    department_id, department, work_section, delivery_group,
                    title, description, assignees_json, status, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task["task_id"],
                    task["item_type"],
                    task.get("project_entity_id"),
                    task.get("project"),
                    task.get('source_project',task.get('project')),
                    task.get("department_id"),
                    task.get("department"),
                    task.get("work_section"),
                    task.get("delivery_group"),
                    task["title"],
                    task["description"],
                    json.dumps(task.get("assignees") or [], ensure_ascii=False),
                    task["status"],
                    task.get("version", 1),
                    now,
                    task.get("updated_at") or now,
                ),
            )
            connection.commit()
