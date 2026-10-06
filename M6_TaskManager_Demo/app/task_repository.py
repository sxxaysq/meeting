"""任务、运行和审计日志的数据访问层。"""

from __future__ import annotations

import json
import sqlite3
import uuid
import hashlib
import re
from pathlib import Path
from typing import Any

from .database import connect, utc_now


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return _decode_task(dict(row)) if row is not None else None


def _decode_task(task: dict) -> dict:
    if "work_items_json" in task:
        raw = task.pop("work_items_json")
        try:
            task["work_items"] = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            task["work_items"] = []
    # 兼容早期把展示字段写在 description 头部的存量数据；新数据使用独立列。
    description = str(task.get("description") or "")
    for field, label in (("department", "部门"), ("project", "项目"), ("priority", "优先级")):
        if not task.get(field):
            match = re.search(rf"(?:^|\n){label}：\s*([^\n]+)", description)
            task[field] = match.group(1).strip() if match else None
    return task


def _nullable_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class TaskRepository:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def create_run(
        self,
        run_id: str,
        meeting_id: str,
        source_file: str,
        meeting_date: str,
        meeting_type: str,
        meeting_title: str = "",
        meeting_time: str = "",
        attendees: str = "",
        leader_requirements: str = "",
    ) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, meeting_id, source_file, meeting_date,
                    meeting_type, meeting_title, meeting_time, attendees,
                    leader_requirements, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    run_id,
                    meeting_id,
                    source_file,
                    meeting_date,
                    meeting_type,
                    meeting_title.strip() or Path(source_file).stem,
                    meeting_time.strip() or None,
                    attendees.strip() or None,
                    leader_requirements.strip() or None,
                    utc_now(),
                ),
            )
            connection.commit()

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "m1_count",
            "m2_count",
            "command_count",
            "applied_count",
            "noop_count",
            "failed_count",
            "summary_json",
            "finished_at",
            "error_summary",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许更新运行字段：{sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [run_id]
        with connect(self.database_path) as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"运行不存在：{run_id}")
            connection.commit()

    def get_run(self, run_id: str) -> dict | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        result = _row_to_dict(row)
        if result and result.get("summary_json"):
            result["summary"] = json.loads(result.pop("summary_json"))
        return result

    def list_meetings(self) -> list[dict]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT meeting_id, source_file, meeting_date, meeting_type,
                       meeting_title, meeting_time, attendees,
                       leader_requirements, started_at
                FROM runs
                ORDER BY started_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_meeting(self, meeting_id: str) -> dict | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT meeting_id, source_file, meeting_date, meeting_type,
                       meeting_title, meeting_time, attendees,
                       leader_requirements, started_at
                FROM runs WHERE meeting_id = ?
                ORDER BY started_at DESC LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_tasks(
        self,
        status: str | None = None,
        include_deleted: bool = False,
        meeting_id: str | None = None,
    ) -> list[dict]:
        where = []
        values: list[Any] = []
        if not include_deleted:
            where.append("is_deleted = 0")
        if status:
            where.append("status = ?")
            values.append(status)
        if meeting_id:
            where.append("source_meeting_id = ?")
            values.append(meeting_id)
        sql = "SELECT * FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY is_deleted, updated_at DESC, task_id"
        with connect(self.database_path) as connection:
            rows = connection.execute(sql, values).fetchall()
        return [_decode_task(dict(row)) for row in rows]

    def get_task(self, task_id: str) -> dict | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_dict(row)

    def get_active_tasks(self) -> list[dict]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE is_deleted = 0 AND status != 'cancelled'
                ORDER BY updated_at DESC, task_id
                """
            ).fetchall()
        return [_decode_task(dict(row)) for row in rows]

    def list_events(
        self,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> list[dict]:
        where = []
        values: list[str] = []
        if run_id:
            where.append("run_id = ?")
            values.append(run_id)
        if task_id:
            where.append("task_id = ?")
            values.append(task_id)
        sql = "SELECT * FROM task_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, event_id DESC"
        with connect(self.database_path) as connection:
            rows = connection.execute(sql, values).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            for field in ("before_json", "after_json"):
                if item.get(field):
                    item[field.removesuffix("_json")] = json.loads(item[field])
                item.pop(field, None)
            results.append(item)
        return results

    def list_task_history(self, task_id: str) -> list[dict]:
        """Return business publication snapshots instead of raw audit events."""
        with connect(self.database_path) as connection:
            if connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone() is None:
                raise KeyError("task not found")
            rows = connection.execute(
                """
                SELECT event_id, db_action, evidence_text, source_meeting_id,
                       after_json, created_at
                FROM task_events
                WHERE task_id = ?
                  AND execution_status = 'applied'
                  AND db_action IN (
                      'CREATE', 'UPDATE_FIELDS', 'UPDATE_STATUS', 'HUMAN_CREATE', 'HUMAN_UPDATE'
                  )
                  AND after_json IS NOT NULL
                """,
                (task_id,),
            ).fetchall()
            meeting_ids = {
                row["source_meeting_id"]
                for row in rows
                if row["source_meeting_id"]
            }
            meetings = {}
            for meeting_id in meeting_ids:
                meeting = connection.execute(
                    """
                    SELECT meeting_id, source_file, meeting_date, meeting_type,
                           meeting_title, meeting_time, started_at
                    FROM runs
                    WHERE meeting_id = ?
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (meeting_id,),
                ).fetchone()
                if meeting is not None:
                    meetings[meeting_id] = dict(meeting)

        history = []
        for row in rows:
            try:
                snapshot = json.loads(row["after_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(snapshot, dict):
                continue
            snapshot = _decode_task(snapshot)
            meeting = meetings.get(row["source_meeting_id"])
            published_at = (
                meeting.get("meeting_date")
                if meeting is not None
                else row["created_at"]
            )
            history.append({
                "event_id": row["event_id"],
                "db_action": row["db_action"],
                "version": snapshot.get("version"),
                "title": snapshot.get("title"),
                "description": snapshot.get("description"),
                "work_items": snapshot.get("work_items") or [],
                "evidence_text": row["evidence_text"],
                "source_meeting_id": row["source_meeting_id"],
                "meeting": meeting,
                "published_at": published_at,
                "recorded_at": row["created_at"],
            })
        history.sort(
            key=lambda item: (
                str(item.get("published_at") or ""),
                str(item.get("recorded_at") or ""),
                str(item.get("event_id") or ""),
            ),
            reverse=True,
        )
        for index, item in enumerate(history):
            item["is_latest_release"] = index == 0
        return history

    def enqueue_review_candidates(
        self,
        run_id: str,
        meeting_id: str,
        candidates: list[dict],
    ) -> int:
        created = 0
        with connect(self.database_path) as connection:
            for index, candidate in enumerate(candidates, start=1):
                if not isinstance(candidate, dict):
                    continue
                source = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(
                    f"{meeting_id}:{candidate.get('candidate_id', index)}:{source}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:20]
                confidence = candidate.get("confidence")
                if not isinstance(confidence, (int, float)):
                    confidence = None
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO review_candidates (
                        candidate_id, run_id, meeting_id, confidence_score,
                        confidence_source, reason_code, candidate_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"R{digest.upper()}",
                        run_id,
                        meeting_id,
                        confidence,
                        str(candidate.get("confidence_source") or "rule"),
                        str(candidate.get("reason") or "needs_review"),
                        source,
                        utc_now(),
                    ),
                )
                created += cursor.rowcount
            connection.commit()
        return created

    def list_review_candidates(self, status: str = "pending") -> list[dict]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_candidates
                WHERE status = ?
                ORDER BY created_at DESC, candidate_id
                """,
                (status,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["candidate"] = json.loads(item.pop("candidate_json"))
            result.append(item)
        return result

    def get_review_candidate(self, candidate_id: str) -> dict | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM review_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["candidate"] = json.loads(item.pop("candidate_json"))
        return item

    def resolve_review_candidate(
        self,
        candidate_id: str,
        status: str,
        task_id: str | None = None,
        reviewer_note: str | None = None,
    ) -> None:
        if status not in {"approved", "rejected"}:
            raise ValueError("invalid review status")
        with connect(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE review_candidates
                SET status = ?, task_id = ?, reviewer_note = ?, reviewed_at = ?
                WHERE candidate_id = ? AND status = 'pending'
                """,
                (status, task_id, reviewer_note, utc_now(), candidate_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("review candidate is not pending")
            connection.commit()

    def update_review_candidate(self, candidate_id: str, fields: dict[str, Any]) -> dict:
        allowed = {
            "title", "description", "work_items", "assignee", "deadline",
            "department", "project", "priority", "initial_status", "reviewer_note", "operations",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许修改复核字段：{sorted(unknown)}")
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM review_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError("review candidate not found")
            if row["status"] != "pending":
                raise KeyError("review candidate already resolved")
            candidate = json.loads(row["candidate_json"])
            for name, value in fields.items():
                if name == "reviewer_note":
                    continue
                candidate[name] = value
            reviewer_note = fields.get("reviewer_note", row["reviewer_note"])
            connection.execute(
                "UPDATE review_candidates SET candidate_json = ?, reviewer_note = ? WHERE candidate_id = ?",
                (json.dumps(candidate, ensure_ascii=False, sort_keys=True), reviewer_note, candidate_id),
            )
            connection.commit()
        return self.get_review_candidate(candidate_id)  # type: ignore[return-value]

    def create_manual_task(self, fields: dict[str, Any]) -> dict:
        now = utc_now()
        task_id = "T" + uuid.uuid4().hex[:10].upper()
        event_id = "E" + uuid.uuid4().hex[:12].upper()
        source_key = f"manual:{uuid.uuid4().hex}"
        evidence = str(fields["evidence_text"]).strip()
        task = {
            "task_id": task_id,
            "title": str(fields["title"]).strip(),
            "description": str(fields.get("description") or "").strip(),
            "work_items_json": json.dumps(fields.get("work_items") or [], ensure_ascii=False),
            "assignee_raw": _nullable_text(fields.get("assignee_raw")),
            "deadline_raw": _nullable_text(fields.get("deadline_raw")),
            "department": _nullable_text(fields.get("department")),
            "project": _nullable_text(fields.get("project")),
            "priority": _nullable_text(fields.get("priority")),
            "status": fields.get("status") or "open",
            "source_key": source_key,
            "source_meeting_id": _nullable_text(fields.get("source_meeting_id")) or "MANUAL",
            "source_segment_id": "manual",
            "source_subsegment_id": task_id,
            "evidence_text": evidence,
            "is_deleted": 0,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
        with connect(self.database_path) as connection:
            connection.execute(
                """INSERT INTO tasks (
                    task_id, title, description, work_items_json, assignee_raw, deadline_raw,
                    department, project, priority, status, source_key, source_meeting_id,
                    source_segment_id, source_subsegment_id, evidence_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(task[name] for name in (
                    "task_id", "title", "description", "work_items_json", "assignee_raw", "deadline_raw",
                    "department", "project", "priority", "status", "source_key", "source_meeting_id",
                    "source_segment_id", "source_subsegment_id", "evidence_text", "created_at", "updated_at",
                )),
            )
            task = dict(connection.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
            connection.execute(
                """INSERT INTO task_events (
                    event_id, event_key, run_id, task_id, source_meeting_id,
                    db_action, execution_status,
                    before_json, after_json, evidence_text, failure_reason, created_at
                ) VALUES (?, ?, 'MANUAL', ?, ?, 'HUMAN_CREATE', 'applied',
                          NULL, ?, ?, NULL, ?)""",
                (event_id, f"manual-create:{task_id}", task_id,
                 task["source_meeting_id"],
                 json.dumps(task, ensure_ascii=False), evidence, now),
            )
            connection.commit()
        return _decode_task(task)

    def update_task_by_human(self, task_id: str, fields: dict[str, Any], *,
                             expected_department: str | None = None,
                             expected_version: int | None = None,
                             progress_note: str | None = None) -> dict:
        allowed = {
            "title", "description", "work_items", "assignee_raw", "deadline_raw",
            "department", "project", "priority", "status",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许修改任务字段：{sorted(unknown)}")
        if not fields:
            raise ValueError("至少提供一个待修改字段")
        fields = dict(fields)
        with connect(self.database_path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("task not found")
            if row["is_deleted"]:
                raise KeyError("task is deleted")
            if expected_department is not None:
                if row['department'] != expected_department:
                    raise KeyError('task is not in this department')
                if set(fields) - {'status'}:
                    raise ValueError('部门处理只能修改状态和追加进展')
                if row['status'] not in {'open', 'in_progress', 'blocked'}:
                    raise ValueError('任务已结束，请刷新待办清单')
            if expected_version is not None and row['version'] != expected_version:
                raise ValueError('任务已被修改，请重新打开并核对最新内容')
            if progress_note:
                previous = row['description'] or ''
                fields['description'] = previous + ('\n' if previous else '') + progress_note
            before = dict(row)
            assignments: list[str] = []
            values: list[Any] = []
            for name, value in fields.items():
                column = "work_items_json" if name == "work_items" else name
                assignments.append(f"{column} = ?")
                values.append(json.dumps(value, ensure_ascii=False) if name == "work_items" else _nullable_text(value))
            now = utc_now()
            assignments.extend(["version = version + 1", "updated_at = ?"])
            values.extend([now, task_id])
            connection.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?", values)
            after = dict(connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
            connection.execute(
                """INSERT INTO task_events (
                    event_id, event_key, run_id, task_id, source_meeting_id,
                    db_action, execution_status,
                    before_json, after_json, evidence_text, failure_reason, created_at
                ) VALUES (?, ?, 'MANUAL', ?, ?, 'HUMAN_UPDATE', 'applied',
                          ?, ?, ?, NULL, ?)""",
                ("E" + uuid.uuid4().hex[:12].upper(), f"manual-update:{uuid.uuid4().hex}", task_id,
                 before["source_meeting_id"],
                 json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                 before["evidence_text"], now),
            )
            connection.commit()
        return _decode_task(after)

    def soft_delete_task_by_human(self, task_id: str) -> dict:
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("task not found")
            if row["is_deleted"]:
                raise KeyError("task already deleted")
            before = dict(row)
            now = utc_now()
            connection.execute(
                """UPDATE tasks SET status = 'cancelled', is_deleted = 1, deleted_at = ?,
                   version = version + 1, updated_at = ? WHERE task_id = ?""",
                (now, now, task_id),
            )
            after = dict(connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
            connection.execute(
                """INSERT INTO task_events (
                    event_id, event_key, run_id, task_id, source_meeting_id,
                    db_action, execution_status,
                    before_json, after_json, evidence_text, failure_reason, created_at
                ) VALUES (?, ?, 'MANUAL', ?, ?, 'HUMAN_SOFT_DELETE', 'applied',
                          ?, ?, ?, NULL, ?)""",
                ("E" + uuid.uuid4().hex[:12].upper(), f"manual-delete:{uuid.uuid4().hex}", task_id,
                 before["source_meeting_id"],
                 json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                 before["evidence_text"], now),
            )
            connection.commit()
        return _decode_task(after)

    def record_failed_event(
        self,
        event_key: str,
        run_id: str,
        evidence_text: str,
        failure_reason: str,
        db_action: str = "M6_VALIDATION",
        task_id: str | None = None,
    ) -> None:
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO task_events (
                    event_id, event_key, run_id, task_id, db_action,
                    execution_status, before_json, after_json,
                    evidence_text, failure_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, 'failed', NULL, NULL, ?, ?, ?)
                """,
                (
                    "E" + uuid.uuid4().hex[:12].upper(),
                    event_key,
                    run_id,
                    task_id,
                    db_action,
                    evidence_text,
                    failure_reason[:1000],
                    utc_now(),
                ),
            )
            connection.commit()
