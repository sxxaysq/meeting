"""SQLite 初始化、连接与演示种子数据。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from .organization import initialize_organization


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    project_id TEXT,
    description TEXT,
    work_items_json TEXT NOT NULL DEFAULT '[]',
    assignee_raw TEXT,
    deadline_raw TEXT,
    department TEXT,
    project TEXT,
    priority TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_progress', 'blocked', 'completed', 'cancelled')),
    source_key TEXT UNIQUE NOT NULL,
    source_meeting_id TEXT NOT NULL,
    source_segment_id TEXT NOT NULL,
    source_subsegment_id TEXT NOT NULL,
    evidence_text TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id TEXT PRIMARY KEY,
    event_key TEXT UNIQUE NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    source_meeting_id TEXT,
    db_action TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    evidence_text TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    meeting_date TEXT NOT NULL,
    meeting_type TEXT NOT NULL,
    meeting_title TEXT NOT NULL DEFAULT '',
    meeting_time TEXT,
    attendees TEXT,
    leader_requirements TEXT,
    status TEXT NOT NULL,
    m1_count INTEGER NOT NULL DEFAULT 0,
    -- Retained for compatibility with existing exported result databases only.
    m2_count INTEGER NOT NULL DEFAULT 0,
    command_count INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    noop_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_summary TEXT
);

CREATE TABLE IF NOT EXISTS review_candidates (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    confidence_score REAL,
    confidence_source TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    task_id TEXT,
    reviewer_note TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, is_deleted);
CREATE INDEX IF NOT EXISTS idx_events_run ON task_events(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_candidates_status
    ON review_candidates(status, created_at);
"""


DEMO_TASKS = (
    {
        "task_id": "T000001",
        "title": "提交3125设备应急方案",
        "description": "机电队提交3125设备应急方案。",
        "assignee_raw": "机电队",
        "deadline_raw": "本周五前",
        "status": "open",
    },
    {
        "task_id": "T000002",
        "title": "完成主井设备检查",
        "description": "运输队完成主井设备检查。",
        "assignee_raw": "运输队",
        "deadline_raw": None,
        "status": "in_progress",
    },
    {
        "task_id": "T000003",
        "title": "整理上月培训台账",
        "description": "培训科整理上月培训台账。",
        "assignee_raw": "培训科",
        "deadline_raw": None,
        "status": "open",
    },
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(database_path: Path | str) -> Iterator[sqlite3.Connection]:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
    finally:
        connection.close()


def initialize_database(
    database_path: Path | str,
    seed_demo_data: bool = False,
) -> None:
    with connect(database_path) as connection:
        connection.executescript(SCHEMA_SQL)
        _migrate_schema(connection)
        initialize_organization(connection)
        if seed_demo_data:
            _seed_demo_tasks(connection)
        connection.commit()


def _migrate_schema(connection: sqlite3.Connection) -> None:
    task_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
    }
    if "work_items_json" not in task_columns:
        connection.execute(
            "ALTER TABLE tasks "
            "ADD COLUMN work_items_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "project_id" not in task_columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_project "
        "ON tasks(project_id, is_deleted)"
    )
    run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
    additions = {
        "meeting_title": "TEXT NOT NULL DEFAULT ''",
        "meeting_time": "TEXT",
        "attendees": "TEXT",
        "leader_requirements": "TEXT",
    }
    for name, definition in additions.items():
        if name not in run_columns:
            connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
    task_additions = {
        "department": "TEXT",
        "project": "TEXT",
        "priority": "TEXT",
    }
    for name, definition in task_additions.items():
        if name not in task_columns:
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

    event_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(task_events)")
    }
    if "source_meeting_id" not in event_columns:
        connection.execute("ALTER TABLE task_events ADD COLUMN source_meeting_id TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_meeting "
        "ON task_events(source_meeting_id, created_at)"
    )
    _backfill_event_meeting_ids(connection)


def _backfill_event_meeting_ids(connection: sqlite3.Connection) -> None:
    """Attach historical events to their source meeting without changing task data."""
    connection.execute(
        """
        UPDATE task_events
        SET source_meeting_id = (
            SELECT runs.meeting_id
            FROM runs
            WHERE runs.run_id = task_events.run_id
            ORDER BY runs.started_at DESC
            LIMIT 1
        )
        WHERE source_meeting_id IS NULL
          AND EXISTS (
              SELECT 1 FROM runs WHERE runs.run_id = task_events.run_id
          )
        """
    )
    unresolved = connection.execute(
        """
        SELECT event_id, event_key, after_json
        FROM task_events
        WHERE source_meeting_id IS NULL
        """
    ).fetchall()
    migration_prefix = "MIGRATION_DUPLICATE_TASK_HISTORY_V1:"
    for row in unresolved:
        meeting_id = None
        event_key = str(row["event_key"] or "")
        if event_key.startswith(migration_prefix):
            source_task_id = event_key[len(migration_prefix):].split("-", 1)[0]
            source_task = connection.execute(
                "SELECT source_meeting_id FROM tasks WHERE task_id = ?",
                (source_task_id,),
            ).fetchone()
            if source_task is not None:
                meeting_id = source_task["source_meeting_id"]
        if meeting_id is None and row["after_json"]:
            try:
                snapshot = json.loads(row["after_json"])
            except (TypeError, json.JSONDecodeError):
                snapshot = {}
            meeting_id = snapshot.get("source_meeting_id")
        if meeting_id:
            connection.execute(
                "UPDATE task_events SET source_meeting_id = ? WHERE event_id = ?",
                (meeting_id, row["event_id"]),
            )


def _seed_demo_tasks(connection: sqlite3.Connection) -> None:
    now = utc_now()
    for task in DEMO_TASKS:
        source_key = f"seed:{task['task_id']}"
        connection.execute(
            """
            INSERT OR IGNORE INTO tasks (
                task_id, title, description, assignee_raw, deadline_raw,
                status, source_key, source_meeting_id, source_segment_id,
                source_subsegment_id, evidence_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'DEMO_SEED', 'seed', '001', ?, ?, ?)
            """,
            (
                task["task_id"],
                task["title"],
                task["description"],
                task["assignee_raw"],
                task["deadline_raw"],
                task["status"],
                source_key,
                json.dumps(task, ensure_ascii=False),
                now,
                now,
            ),
        )
