"""合并已落库的“智能综合管控平台”过细任务，保留完整审计。"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import connect, initialize_database, utc_now


CANONICAL_TASK_ID = "T3B481F2C74"
SOURCE_TASK_IDS = (
    "T3B481F2C74",
    "T686484A808",
    "TAE6AF42B4C",
    "TA20AE209E8",
)
TITLE = "构建智能综合管控平台"
WORK_ITEMS = [
    "设计灾害预警系统原型 1 版",
    "设计综合大屏可视化原型 1 版",
    "推进流程中心功能测试及 bug 修复",
    "推进流程设计引擎研发等后续工作",
]
MIGRATION_RUN_ID = "MIGRATION_TASK_GRANULARITY_V1"
EVENT_PREFIX = "migration:task-granularity-v1"


def _event_id() -> str:
    return "E" + uuid.uuid4().hex[:12].upper()


def _insert_event(
    connection,
    event_key: str,
    task_id: str,
    action: str,
    before: dict,
    after: dict,
    evidence_text: str,
    reason: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO task_events (
            event_id, event_key, run_id, task_id, db_action,
            execution_status, before_json, after_json,
            evidence_text, failure_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, 'applied', ?, ?, ?, ?, ?)
        """,
        (
            _event_id(),
            event_key,
            MIGRATION_RUN_ID,
            task_id,
            action,
            json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False),
            evidence_text,
            reason,
            utc_now(),
        ),
    )


def migrate(database_path: Path | str) -> dict:
    initialize_database(database_path)
    marks = ",".join("?" for _ in SOURCE_TASK_IDS)
    canonical_event_key = f"{EVENT_PREFIX}:{CANONICAL_TASK_ID}"
    with connect(database_path) as connection:
        if connection.execute(
            "SELECT 1 FROM task_events WHERE event_key = ?",
            (canonical_event_key,),
        ).fetchone():
            return {
                "status": "already_applied",
                "canonical_task_id": CANONICAL_TASK_ID,
            }

        rows = connection.execute(
            f"SELECT * FROM tasks WHERE task_id IN ({marks})",
            SOURCE_TASK_IDS,
        ).fetchall()
        if len(rows) != len(SOURCE_TASK_IDS):
            found = {row["task_id"] for row in rows}
            missing = sorted(set(SOURCE_TASK_IDS) - found)
            raise RuntimeError(f"迁移目标任务缺失：{missing}")

        tasks = {row["task_id"]: dict(row) for row in rows}
        source_keys = {
            (
                task["source_meeting_id"],
                task["source_segment_id"],
                task["source_subsegment_id"],
            )
            for task in tasks.values()
        }
        assignees = {task["assignee_raw"] for task in tasks.values()}
        evidences = {task["evidence_text"] for task in tasks.values()}
        if len(source_keys) != 1 or len(assignees) != 1 or len(evidences) != 1:
            raise RuntimeError("迁移目标不属于同一来源、责任主体和原文证据")
        if any(task["is_deleted"] for task in tasks.values()):
            raise RuntimeError("迁移目标中已有软删除任务，拒绝部分重复迁移")

        now = utc_now()
        evidence_text = next(iter(evidences))
        connection.execute("BEGIN IMMEDIATE")

        canonical_before = tasks[CANONICAL_TASK_ID]
        connection.execute(
            """
            UPDATE tasks
            SET title = ?, description = ?, work_items_json = ?,
                version = version + 1, updated_at = ?
            WHERE task_id = ?
            """,
            (
                TITLE,
                evidence_text.strip(),
                json.dumps(WORK_ITEMS, ensure_ascii=False),
                now,
                CANONICAL_TASK_ID,
            ),
        )
        canonical_after = dict(
            connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (CANONICAL_TASK_ID,),
            ).fetchone()
        )
        _insert_event(
            connection,
            canonical_event_key,
            CANONICAL_TASK_ID,
            "GRANULARITY_MERGE",
            canonical_before,
            canonical_after,
            evidence_text,
            "四个工作环节归并为一个可独立验收的业务目标任务",
        )

        retired: list[str] = []
        for task_id in SOURCE_TASK_IDS:
            if task_id == CANONICAL_TASK_ID:
                continue
            before = tasks[task_id]
            connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', is_deleted = 1, deleted_at = ?,
                    version = version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (now, now, task_id),
            )
            after = dict(
                connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
            )
            _insert_event(
                connection,
                f"{EVENT_PREFIX}:{task_id}",
                task_id,
                "MERGED_INTO",
                before,
                after,
                evidence_text,
                f"已合并至主任务 {CANONICAL_TASK_ID}",
            )
            retired.append(task_id)
        connection.commit()
    return {
        "status": "applied",
        "canonical_task_id": CANONICAL_TASK_ID,
        "title": TITLE,
        "work_items": WORK_ITEMS,
        "retired_task_ids": retired,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/demo.db"),
    )
    args = parser.parse_args()
    print(json.dumps(migrate(args.database), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
