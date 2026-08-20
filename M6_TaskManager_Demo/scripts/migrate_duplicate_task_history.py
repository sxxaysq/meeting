"""Replay duplicate project tasks as updates and soft-delete the duplicates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import connect, initialize_database
from app.m6.executor import CommandExecutor
from app.m6.model_client import empty_patch
from app.m6.validator import CommandValidator


RUN_ID = "MIGRATION_DUPLICATE_TASK_HISTORY_V1"
MEETING_ID = "MIGRATION_DUPLICATE_TASK_HISTORY_V1"


def _load_task(database: Path, task_id: str) -> dict:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        raise ValueError("任务不存在：{}".format(task_id))
    return dict(row)


def _candidate_view(task: dict) -> dict:
    return {
        "task_id": task["task_id"],
        "title": task["title"],
        "description": task.get("description"),
        "work_items": json.loads(task.get("work_items_json") or "[]"),
        "assignee_raw": task.get("assignee_raw"),
        "deadline_raw": task.get("deadline_raw"),
        "status": task["status"],
        "version": task["version"],
        "project_id": task.get("project_id"),
    }


def _batch(source_key: str, command: dict) -> dict:
    return {
        "schema_version": "m6_db_command_v1",
        "source_key": source_key,
        "commands": [command],
    }


def _source(task: dict, suffix: str) -> dict:
    return {
        "segment_id": "{}-{}".format(task["task_id"], suffix),
        "subsegment_id": "001",
        "text": task["evidence_text"],
    }


def _update_batch(canonical: dict, duplicate: dict) -> tuple[dict, dict, dict]:
    source = _source(duplicate, "update")
    source_key = "{}:{}:001".format(MEETING_ID, source["segment_id"])
    target = _candidate_view(canonical)
    context = {
        "candidates": [target],
        "unique_target_task_id": canonical["task_id"],
        "match_mode": "migration_exact_project_task",
    }
    command = {
        "command_index": 1,
        "db_action": "UPDATE_FIELDS",
        "target_task_id": canonical["task_id"],
        "expected_version": canonical["version"],
        "task_patch": empty_patch(
            title=duplicate["title"],
            description=duplicate["description"],
            work_items=json.loads(
                duplicate.get("work_items_json") or "[]"
            ),
            assignee_raw=(
                duplicate["assignee_raw"]
                or canonical.get("assignee_raw")
            ),
            deadline_raw=(
                duplicate["deadline_raw"]
                or canonical.get("deadline_raw")
            ),
        ),
        "evidence": {
            "segment_id": source["segment_id"],
            "subsegment_id": source["subsegment_id"],
            "text": duplicate["evidence_text"],
        },
        "reason_code": "MIGRATED_DUPLICATE_AS_PROGRESS_UPDATE",
        "ambiguities": [],
    }
    return _batch(source_key, command), source, context


def _delete_batch(duplicate: dict) -> tuple[dict, dict, dict]:
    source = _source(duplicate, "delete")
    source_key = "{}:{}:001".format(MEETING_ID, source["segment_id"])
    target = _candidate_view(duplicate)
    context = {
        "candidates": [target],
        "unique_target_task_id": duplicate["task_id"],
        "match_mode": "migration_duplicate_soft_delete",
    }
    command = {
        "command_index": 1,
        "db_action": "SOFT_DELETE",
        "target_task_id": duplicate["task_id"],
        "expected_version": duplicate["version"],
        "task_patch": empty_patch(),
        "evidence": {
            "segment_id": source["segment_id"],
            "subsegment_id": source["subsegment_id"],
            "text": duplicate["evidence_text"],
        },
        "reason_code": "MIGRATED_DUPLICATE_SOFT_DELETE",
        "ambiguities": [],
    }
    return _batch(source_key, command), source, context


def migrate(
    database: Path,
    project_id: str,
    canonical_task_id: str,
    duplicate_task_ids: list[str],
    apply: bool = False,
) -> dict:
    initialize_database(database)
    if canonical_task_id in duplicate_task_ids:
        raise ValueError("主任务不能同时出现在重复任务列表")
    if len(duplicate_task_ids) != len(set(duplicate_task_ids)):
        raise ValueError("重复任务列表中存在重复 ID")

    canonical = _load_task(database, canonical_task_id)
    duplicates = [_load_task(database, task_id) for task_id in duplicate_task_ids]
    tasks = [canonical, *duplicates]
    if any(task.get("project_id") != project_id for task in tasks):
        raise ValueError("待迁移任务不属于指定项目")
    if any(task["is_deleted"] for task in tasks):
        raise ValueError("待迁移任务中包含已软删除任务")

    plan = {
        "status": "planned" if not apply else "applying",
        "project_id": project_id,
        "canonical_task_id": canonical_task_id,
        "duplicate_task_ids": duplicate_task_ids,
        "update_order": [
            {
                "task_id": task["task_id"],
                "title": task["title"],
                "source_meeting_id": task["source_meeting_id"],
                "created_at": task["created_at"],
            }
            for task in duplicates
        ],
    }
    if not apply:
        return plan

    validator = CommandValidator()
    executor = CommandExecutor(database)
    update_results = []
    delete_results = []
    for duplicate in duplicates:
        canonical = _load_task(database, canonical_task_id)
        batch, source, context = _update_batch(canonical, duplicate)
        validator.validate_batch(batch, source, MEETING_ID, context)
        update_results.extend(
            executor.execute_batch(batch, RUN_ID, MEETING_ID)
        )

        duplicate = _load_task(database, duplicate["task_id"])
        batch, source, context = _delete_batch(duplicate)
        validator.validate_batch(batch, source, MEETING_ID, context)
        delete_results.extend(
            executor.execute_batch(batch, RUN_ID, MEETING_ID)
        )

    if any(
        result["execution_status"] != "applied"
        for result in [*update_results, *delete_results]
    ):
        raise RuntimeError("迁移命令未全部成功应用")
    plan.update({
        "status": "applied",
        "canonical_after": _load_task(database, canonical_task_id),
        "update_results": update_results,
        "delete_results": delete_results,
    })
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将同项目重复任务重放为主任务更新并软删除重复项"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--canonical-task-id", required=True)
    parser.add_argument(
        "--duplicate-task-id",
        action="append",
        dest="duplicate_task_ids",
        required=True,
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = migrate(
        args.database,
        args.project_id,
        args.canonical_task_id,
        args.duplicate_task_ids,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
