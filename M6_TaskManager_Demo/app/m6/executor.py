"""受约束 M6 命令的事务执行器。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from ..database import connect, utc_now
from ..review_plan import validate_operations
from .model_client import empty_patch


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

    def execute_review(self, candidate_id: str, operations: list, reviewer_note: str = '') -> dict:
        """Apply all human-approved children and resolve their parent atomically."""
        with connect(self.database_path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM review_candidates WHERE candidate_id=?', (candidate_id,)).fetchone()
            if row is None:
                raise KeyError('复核记录不存在')
            candidate = json.loads(row['candidate_json'])
            if row['status'] != 'pending':
                if row['status'] == 'approved' and candidate.get('operations') == operations and candidate.get('review_results'):
                    return {'execution_status': 'duplicate', 'results': candidate['review_results']}
                raise CommandExecutionError('复核记录已处理，不能再次提交不同方案')
            validate_operations(operations, candidate)
            evidence = str(candidate.get('evidence') or candidate.get('source_text') or '').strip()
            if not evidence:
                raise ValueError('复核记录缺少原文证据')
            results = []
            for index, op in enumerate(operations, 1):
                target = op.get('target_task_id')
                description = op['description'].strip()
                work_items = []
                if target:
                    task = connection.execute('SELECT * FROM tasks WHERE task_id=?', (target,)).fetchone()
                    if task is None:
                        raise CommandExecutionError('关联任务不存在，请重新选择')
                    previous = task['description'] or ''
                    if description not in previous:
                        description = previous + ('\n' if previous else '') + description
                    else:
                        description = previous
                    work_items = json.loads(task['work_items_json'] or '[]')
                work_items = list(dict.fromkeys([*work_items, op['description'].strip()]))
                source_key = f"{row['meeting_id']}:review_{candidate_id}:split"
                command = {'command_index': index, 'db_action': op['action'], 'target_task_id': target,
                           'expected_version': op.get('expected_version'),
                           'task_patch': empty_patch(title=op['title'].strip(), description=description,
                               work_items=work_items, assignee_raw=op['assignee_raw'],
                               deadline_raw=op['deadline_raw'], status=op['status']),
                           'evidence': {'segment_id': f'review_{candidate_id}', 'subsegment_id': str(index), 'text': evidence}}
                result = self._apply(connection, source_key, command, row['meeting_id'])
                task_id = result['task_id']
                connection.execute('UPDATE tasks SET department=?,project=?,priority=?,status=? WHERE task_id=?',
                                   (op['department'] or None, op['project'] or None, op['priority'] or None, op['status'], task_id))
                if result['before'] and result['before'].get('project') != (op['project'] or None):
                    connection.execute('UPDATE tasks SET project_id=NULL WHERE task_id=?', (task_id,))
                result['after'] = dict(connection.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
                event_id = _event_id()
                connection.execute('''INSERT INTO task_events
                    (event_id,event_key,run_id,task_id,source_meeting_id,db_action,execution_status,
                     before_json,after_json,evidence_text,failure_reason,created_at)
                    VALUES (?,?,?,?,?,?,'applied',?,?,?,NULL,?)''',
                    (event_id, f'{source_key}:{index}', row['run_id'], task_id, row['meeting_id'], op['action'],
                     json.dumps(result['before'], ensure_ascii=False) if result['before'] else None,
                     json.dumps(result['after'], ensure_ascii=False), evidence, utc_now()))
                results.append({'task_id': task_id, 'event_id': event_id, 'db_action': op['action'], 'execution_status': 'applied'})
            candidate.update(operations=operations, review_results=results)
            connection.execute('''UPDATE review_candidates SET status='approved',task_id=?,reviewer_note=?,
                reviewed_at=?,candidate_json=? WHERE candidate_id=?''',
                (results[0]['task_id'], reviewer_note or None, utc_now(), json.dumps(candidate, ensure_ascii=False), candidate_id))
            connection.commit()
            return {'execution_status': 'applied', 'results': results}

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
