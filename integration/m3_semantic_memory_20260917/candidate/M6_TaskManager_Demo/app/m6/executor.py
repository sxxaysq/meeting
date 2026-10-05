"""受约束 M6 命令的事务执行器。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from ..database import connect, utc_now


class CommandExecutionError(RuntimeError):
    """命令在数据库边界校验或事务执行时失败。"""


def _task_id() -> str:
    return "T" + uuid.uuid4().hex[:10].upper()


def _event_id() -> str:
    return "E" + uuid.uuid4().hex[:12].upper()


def _task_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class CommandExecutor:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    def execute_batch(
        self,
        batch: dict,
        run_id: str,
        meeting_id: str,
    ) -> list[dict]:
        results = []
        for command in batch["commands"]:
            results.append(
                self.execute_command(
                    source_key=batch["source_key"],
                    command=command,
                    run_id=run_id,
                    meeting_id=meeting_id,
                )
            )
        return results

    def execute_command(
        self,
        source_key: str,
        command: dict,
        run_id: str,
        meeting_id: str,
    ) -> dict:
        event_key = f"{source_key}:{command['command_index']}"
        evidence_text = command["evidence"]["text"]
        with connect(self.database_path) as connection:
            existing_event = connection.execute(
                "SELECT * FROM task_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if existing_event:
                duplicate_event_key = f"{event_key}:duplicate:{run_id}"
                duplicate = connection.execute(
                    "SELECT * FROM task_events WHERE event_key = ?",
                    (duplicate_event_key,),
                ).fetchone()
                if duplicate is None:
                    duplicate_event_id = _event_id()
                    connection.execute(
                        """
                        INSERT INTO task_events (
                            event_id, event_key, run_id, task_id,
                            source_meeting_id, db_action,
                            execution_status, before_json, after_json,
                            evidence_text, failure_reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'duplicate', ?, ?, ?, ?, ?)
                        """,
                        (
                            duplicate_event_id,
                            duplicate_event_key,
                            run_id,
                            existing_event["task_id"],
                            existing_event["source_meeting_id"],
                            existing_event["db_action"],
                            existing_event["before_json"],
                            existing_event["after_json"],
                            evidence_text,
                            "相同会议来源命令已执行，本次未重复写入任务",
                            utc_now(),
                        ),
                    )
                    connection.commit()
                else:
                    duplicate_event_id = duplicate["event_id"]
                return {
                    "event_id": duplicate_event_id,
                    "event_key": duplicate_event_key,
                    "task_id": existing_event["task_id"],
                    "db_action": existing_event["db_action"],
                    "execution_status": "duplicate",
                }

            try:
                connection.execute("BEGIN IMMEDIATE")
                result = self._apply(
                    connection,
                    source_key=source_key,
                    command=command,
                    meeting_id=meeting_id,
                )
                event_id = _event_id()
                connection.execute(
                    """
                    INSERT INTO task_events (
                        event_id, event_key, run_id, task_id,
                        source_meeting_id, db_action,
                        execution_status, before_json, after_json,
                        evidence_text, failure_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        event_id,
                        event_key,
                        run_id,
                        result.get("task_id"),
                        meeting_id,
                        command["db_action"],
                        result["execution_status"],
                        json.dumps(result.get("before"), ensure_ascii=False)
                        if result.get("before") is not None
                        else None,
                        json.dumps(result.get("after"), ensure_ascii=False)
                        if result.get("after") is not None
                        else None,
                        evidence_text,
                        utc_now(),
                    ),
                )
                connection.commit()
                return {
                    "event_id": event_id,
                    "event_key": event_key,
                    "db_action": command["db_action"],
                    **result,
                }
            except Exception as exc:
                connection.rollback()
                failure = str(exc)[:500]
                connection.execute("BEGIN IMMEDIATE")
                event_id = _event_id()
                connection.execute(
                    """
                    INSERT INTO task_events (
                        event_id, event_key, run_id, task_id,
                        source_meeting_id, db_action,
                        execution_status, before_json, after_json,
                        evidence_text, failure_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'failed', NULL, NULL, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_key,
                        run_id,
                        command.get("target_task_id"),
                        meeting_id,
                        command["db_action"],
                        evidence_text,
                        failure,
                        utc_now(),
                    ),
                )
                connection.commit()
                return {
                    "event_id": event_id,
                    "event_key": event_key,
                    "task_id": command.get("target_task_id"),
                    "db_action": command["db_action"],
                    "execution_status": "failed",
                    "failure_reason": failure,
                }

    def _apply(
        self,
        connection: sqlite3.Connection,
        source_key: str,
        command: dict,
        meeting_id: str,
    ) -> dict:
        action = command["db_action"]
        if action == "NOOP":
            return {
                "task_id": None,
                "execution_status": "noop",
                "before": None,
                "after": None,
            }
        if action == "CREATE":
            task_source_key = f"{source_key}:{command['command_index']}"
            return self._create(
                connection, task_source_key, command, meeting_id
            )
        return self._update(connection, command)

    def _create(
        self,
        connection: sqlite3.Connection,
        source_key: str,
        command: dict,
        meeting_id: str,
    ) -> dict:
        existing = connection.execute(
            "SELECT * FROM tasks WHERE source_key = ?", (source_key,)
        ).fetchone()
        if existing:
            return {
                "task_id": existing["task_id"],
                "execution_status": "noop",
                "before": dict(existing),
                "after": dict(existing),
            }
        patch = command["task_patch"]
        now = utc_now()
        task_id = _task_id()
        evidence = command["evidence"]
        connection.execute(
            """
            INSERT INTO tasks (
                task_id, title, description, work_items_json,
                assignee_raw, deadline_raw,
                status, source_key, source_meeting_id, source_segment_id,
                source_subsegment_id, evidence_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                patch["title"].strip(),
                patch["description"],
                json.dumps(patch["work_items"] or [], ensure_ascii=False),
                patch["assignee_raw"],
                patch["deadline_raw"],
                patch["status"] or "open",
                source_key,
                meeting_id,
                evidence["segment_id"],
                evidence["subsegment_id"],
                evidence["text"],
                now,
                now,
            ),
        )
        after = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return {
            "task_id": task_id,
            "execution_status": "applied",
            "before": None,
            "after": dict(after),
        }

    def _update(
        self,
        connection: sqlite3.Connection,
        command: dict,
    ) -> dict:
        task_id = command["target_task_id"]
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise CommandExecutionError(f"目标任务不存在：{task_id}")
        before = dict(row)
        if before["is_deleted"]:
            raise CommandExecutionError(f"目标任务已删除：{task_id}")
        if before["version"] != command["expected_version"]:
            raise CommandExecutionError(
                f"任务版本冲突：期望 {command['expected_version']}，"
                f"当前 {before['version']}"
            )

        action = command["db_action"]
        now = utc_now()
        if action == "UPDATE_FIELDS":
            patch = command["task_patch"]
            fields = {
                name: value
                for name, value in patch.items()
                if value is not None and name != "status"
            }
            if "work_items" in fields:
                fields["work_items_json"] = json.dumps(
                    fields.pop("work_items"),
                    ensure_ascii=False,
                )
            assignments = ", ".join(f"{name} = ?" for name in fields)
            connection.execute(
                f"""
                UPDATE tasks
                SET {assignments}, version = version + 1, updated_at = ?
                WHERE task_id = ? AND version = ?
                """,
                [*fields.values(), now, task_id, command["expected_version"]],
            )
        elif action == "UPDATE_STATUS":
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, version = version + 1, updated_at = ?
                WHERE task_id = ? AND version = ?
                """,
                (
                    command["task_patch"]["status"],
                    now,
                    task_id,
                    command["expected_version"],
                ),
            )
        elif action == "SOFT_DELETE":
            connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', is_deleted = 1, deleted_at = ?,
                    version = version + 1, updated_at = ?
                WHERE task_id = ? AND version = ?
                """,
                (now, now, task_id, command["expected_version"]),
            )
        else:
            raise CommandExecutionError(f"不支持的数据库动作：{action}")

        after_row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        after = _task_dict(after_row)
        if after is None or after["version"] != before["version"] + 1:
            raise CommandExecutionError("任务更新未生效")
        return {
            "task_id": task_id,
            "execution_status": "applied",
            "before": before,
            "after": after,
        }
