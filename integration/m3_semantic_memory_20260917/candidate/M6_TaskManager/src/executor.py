"""Transactional executor for validated lifecycle commands."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .models import (
    ExecutionConflict,
    LifecycleAction,
    TaskCommand,
    TaskStatus,
)
from .repository import TaskRepository, decode_task, utc_now
from .state_machine import next_status


def _identifier(prefix: str, length: int = 16) -> str:
    return prefix + uuid.uuid4().hex[:length].upper()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def description_with_progress(description: str, content: str) -> str:
    """Keep the task's requirements and append source-backed progress once."""
    content = content.strip()
    if not content or content in description:
        return description
    return description + ("\n" if description else "") + content


class TaskExecutor:
    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

    def execute(self, command: TaskCommand) -> dict[str, Any]:
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT result_json FROM processing_records
                WHERE source_document_id = ? AND source_item_id = ?
                """,
                (command.source_document_id, command.source_item_id),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                result = json.loads(existing["result_json"])
                return {**result, "execution_status": "DUPLICATE"}
            try:
                if command.action in (LifecycleAction.SKIP, LifecycleAction.ROUTE_M4):
                    result = self._skip(connection, command)
                elif command.action is LifecycleAction.REVIEW:
                    result = self._review(connection, command)
                elif command.action is LifecycleAction.CREATE:
                    result = self._create(connection, command)
                else:
                    result = self._apply_existing(connection, command)
                self._record_processing(connection, command, result)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def execute_multi(self, commands: list[TaskCommand]) -> dict[str, Any]:
        """Apply a MULTI item's validated sub-commands in one transaction.

        All sub-commands of one item share (source_document_id, source_item_id),
        so they cannot go through ``execute`` one by one — the second would read
        the first's processing record and no-op as DUPLICATE. One transaction
        keeps the multi-goal item atomic and writes exactly one processing
        record, preserving idempotent replay.
        """
        if not commands:
            raise ValueError("execute_multi 需要至少一条命令")
        first = commands[0]
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT result_json FROM processing_records
                WHERE source_document_id = ? AND source_item_id = ?
                """,
                (first.source_document_id, first.source_item_id),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                result = json.loads(existing["result_json"])
                return {**result, "execution_status": "DUPLICATE"}
            try:
                sub_results = []
                for command in commands:
                    if command.action in (LifecycleAction.SKIP, LifecycleAction.ROUTE_M4):
                        sub_results.append(self._skip(connection, command))
                    elif command.action is LifecycleAction.REVIEW:
                        sub_results.append(self._review(connection, command))
                    elif command.action is LifecycleAction.CREATE:
                        sub_results.append(self._create(connection, command))
                    else:
                        sub_results.append(self._apply_existing(connection, command))
                result = {
                    "execution_status": "APPLIED",
                    "action": "MULTI",
                    "task_id": None,
                    "event_id": None,
                    "sub_results": sub_results,
                }
                self._record_processing(connection, first, result, action="MULTI")
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def _skip(
        self,
        connection: sqlite3.Connection,
        command: TaskCommand,
    ) -> dict[str, Any]:
        audit_id = _identifier("A")
        now = utc_now()
        connection.execute(
            """
            INSERT INTO task_audit (
                audit_id, task_id, action, before_state_json,
                after_state_json, reason, source_document_id,
                source_item_id, provenance_json, created_at
            ) VALUES (?, NULL, ?, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                command.action.value,
                command.reason,
                command.source_document_id,
                command.source_item_id,
                _json(command.provenance),
                now,
            ),
        )
        return {
            "execution_status": "ROUTED" if command.action is LifecycleAction.ROUTE_M4 else "SKIPPED",
            "action": command.action.value,
            "task_id": None,
            "event_id": None,
            "audit_id": audit_id,
        }

    def _review(
        self,
        connection: sqlite3.Connection,
        command: TaskCommand,
    ) -> dict[str, Any]:
        review_id = _identifier("R")
        audit_id = _identifier("A")
        now = utc_now()
        connection.execute(
            """
            INSERT INTO lifecycle_reviews (
                review_id, source_document_id, source_item_id,
                item_json, candidates_json, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                command.source_document_id,
                command.source_item_id,
                _json(command.provenance.get("item", {})),
                _json(command.provenance.get("candidates", [])),
                command.reason,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_audit (
                audit_id, task_id, action, before_state_json,
                after_state_json, reason, source_document_id,
                source_item_id, provenance_json, created_at
            ) VALUES (?, NULL, 'REVIEW', NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                command.reason,
                command.source_document_id,
                command.source_item_id,
                _json(command.provenance),
                now,
            ),
        )
        return {
            "execution_status": "REVIEW_QUEUED",
            "action": "REVIEW",
            "task_id": None,
            "event_id": None,
            "review_id": review_id,
            "audit_id": audit_id,
        }

    def _create(
        self,
        connection: sqlite3.Connection,
        command: TaskCommand,
    ) -> dict[str, Any]:
        task_id = _identifier("TASK-")
        now = utc_now()
        changes = command.changes
        connection.execute(
            """
            INSERT INTO tasks (
                task_id, item_type, project_entity_id, project, source_project,
                department_id, department, work_section, delivery_group,
                title, description, assignees_json, status, version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                task_id,
                changes["item_type"],
                changes.get("project_entity_id"),
                changes.get("project"),
                changes.get('source_project',changes.get('project')),
                changes["department_id"],
                changes["department"],
                changes.get("work_section"),
                changes.get("delivery_group"),
                changes["title"],
                changes["description"],
                _json(changes.get("assignees") or []),
                changes.get("status", TaskStatus.OPEN.value),
                now,
                now,
            ),
        )
        after_row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        after = decode_task(after_row)
        event_id, audit_id, dispatch_id = self._record_side_effects(
            connection,
            command,
            task_id=task_id,
            before=None,
            after=after,
            department_route=changes["department_route"],
        )
        return {
            "execution_status": "APPLIED",
            "action": command.action.value,
            "task_id": task_id,
            "event_id": event_id,
            "audit_id": audit_id,
            "dispatch_id": dispatch_id,
            "version": 1,
        }

    def _apply_existing(
        self,
        connection: sqlite3.Connection,
        command: TaskCommand,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (command.target_task_id,),
        ).fetchone()
        if row is None:
            raise ExecutionConflict("目标任务不存在")
        before = decode_task(row)
        if before["version"] != command.expected_version:
            raise ExecutionConflict(
                f"任务版本冲突：期望 {command.expected_version}，"
                f"当前 {before['version']}"
            )
        action = command.action
        next_status(before['status'],action)  # Commit-time state check, after authoritative reread.
        now = utc_now()
        route = command.changes["department_route"]
        if command.changes.get('project_entity_id'):
            scope=command.provenance.get('admission',{})
            if not scope.get('scope_validated') or command.changes.get('project') != command.provenance['item'].get('project'):
                raise ExecutionConflict('项目归属缺少M6校验依据')
            connection.execute('UPDATE tasks SET project=?,project_entity_id=? WHERE task_id=? AND version=?',
                (command.changes['project'],command.changes['project_entity_id'],before['task_id'],before['version']))
        if action is LifecycleAction.PROGRESS_UPDATE:
            connection.execute('UPDATE tasks SET version=version+1, updated_at=? WHERE task_id=? AND version=?',
                               (now,before['task_id'],before['version']))
            after = self._load_after(connection,before['task_id'])
        elif action is LifecycleAction.MODIFY:
            self._update_fields(connection, before, command.changes, now)
            after = self._load_after(connection, before["task_id"])
        elif action in {
            LifecycleAction.COMPLETE,
            LifecycleAction.CANCEL,
            LifecycleAction.REOPEN,
        }:
            status = {
                LifecycleAction.COMPLETE: TaskStatus.COMPLETED.value,
                LifecycleAction.CANCEL: TaskStatus.CANCELLED.value,
                LifecycleAction.REOPEN: TaskStatus.IN_PROGRESS.value,
            }[action]
            self._update_status(connection, before, status, now)
            after = self._load_after(connection, before["task_id"])
        elif action is LifecycleAction.TRANSFER:
            connection.execute(
                """
                UPDATE tasks
                SET department_id = ?, department = ?,
                    version = version + 1, updated_at = ?
                WHERE task_id = ? AND version = ?
                """,
                (
                    command.changes["department_id"],
                    command.changes["department"],
                    now,
                    before["task_id"],
                    before["version"],
                ),
            )
            after = self._load_after(connection, before["task_id"])
        else:
            raise ExecutionConflict(f"不支持动作：{action.value}")

        if action in {LifecycleAction.PROGRESS_UPDATE, LifecycleAction.MODIFY, LifecycleAction.TRANSFER}:
            description = description_with_progress(after["description"], command.event_content)
            connection.execute("UPDATE tasks SET description = ? WHERE task_id = ?",
                               (description, before["task_id"]))
            after["description"] = description

        event_id, audit_id, dispatch_id = self._record_side_effects(
            connection,
            command,
            task_id=before["task_id"],
            before=before,
            after=after,
            department_route=route,
        )
        return {
            "execution_status": "APPLIED",
            "action": action.value,
            "task_id": before["task_id"],
            "event_id": event_id,
            "audit_id": audit_id,
            "dispatch_id": dispatch_id,
            "version": after["version"],
        }

    @staticmethod
    def _update_fields(
        connection: sqlite3.Connection,
        before: dict[str, Any],
        changes: dict[str, Any],
        now: str,
    ) -> None:
        allowed = {
            "title": "title",
            "description": "description",
            "work_section": "work_section",
            "delivery_group": "delivery_group",
            "assignees": "assignees_json",
        }
        fields = {
            allowed[name]: (_json(value) if name == "assignees" else value)
            for name, value in changes.items()
            if name in allowed
        }
        if not fields:
            raise ExecutionConflict("MODIFY 没有可执行字段")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        cursor = connection.execute(
            f"""
            UPDATE tasks
            SET {assignments}, version = version + 1, updated_at = ?
            WHERE task_id = ? AND version = ?
            """,
            [*fields.values(), now, before["task_id"], before["version"]],
        )
        if cursor.rowcount != 1:
            raise ExecutionConflict("MODIFY 版本条件未命中")

    @staticmethod
    def _update_status(
        connection: sqlite3.Connection,
        before: dict[str, Any],
        status: str,
        now: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE tasks
            SET status = ?, version = version + 1, updated_at = ?
            WHERE task_id = ? AND version = ?
            """,
            (status, now, before["task_id"], before["version"]),
        )
        if cursor.rowcount != 1:
            raise ExecutionConflict("状态更新版本条件未命中")

    @staticmethod
    def _load_after(
        connection: sqlite3.Connection,
        task_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise ExecutionConflict("更新后任务不存在")
        return decode_task(row)

    def _record_side_effects(
        self,
        connection: sqlite3.Connection,
        command: TaskCommand,
        *,
        task_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        department_route: str,
    ) -> tuple[str, str, str]:
        event_id = _identifier("EVT-")
        link_id = _identifier("LNK-")
        audit_id = _identifier("AUD-")
        dispatch_id = _identifier("DSP-")
        now = utc_now()
        item = command.provenance["item"]
        connection.execute(
            """
            INSERT INTO task_events (
                event_id, task_id, event_type, content,
                source_document_id, source_item_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                command.action.value,
                command.event_content,
                command.source_document_id,
                command.source_item_id,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_source_links (
                link_id, task_id, event_id, source_document_id,
                source_item_id, evidence_json, provenance_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link_id,
                task_id,
                event_id,
                command.source_document_id,
                command.source_item_id,
                _json(item.get("evidence", {})),
                _json(command.provenance),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO task_audit (
                audit_id, task_id, action, before_state_json,
                after_state_json, reason, source_document_id,
                source_item_id, provenance_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                task_id,
                command.action.value,
                _json(before) if before is not None else None,
                _json(after),
                command.reason,
                command.source_document_id,
                command.source_item_id,
                _json(command.provenance),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO dispatch_queue (
                dispatch_id, task_id, department_id, department,
                route, source_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dispatch_id,
                task_id,
                after["department_id"],
                after["department"],
                department_route,
                event_id,
                now,
            ),
        )
        return event_id, audit_id, dispatch_id

    @staticmethod
    def _record_processing(
        connection: sqlite3.Connection,
        command: TaskCommand,
        result: dict[str, Any],
        action: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO processing_records (
                processing_id, source_document_id, source_item_id,
                action, task_id, event_id, result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _identifier("PRC-"),
                command.source_document_id,
                command.source_item_id,
                action or command.action.value,
                result.get("task_id"),
                result.get("event_id"),
                _json(result),
                utc_now(),
            ),
        )
