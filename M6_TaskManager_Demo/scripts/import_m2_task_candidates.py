"""将新版 M2 项目级候选任务通过 M6 事务执行器写入 SQLite。"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import connect, initialize_database, utc_now
from app.m6.executor import CommandExecutor
from app.m6.model_client import empty_patch
from app.m6.project_task_matcher import ProjectTaskMatcher
from app.m6.validator import CommandValidator


STATUS_MAP = {
    "待执行": "open",
    "进行中": "in_progress",
    "已完成": "completed",
    "已延期": "blocked",
}


def meeting_id_for(source_file):
    stem = Path(source_file or "m1_tasks").stem
    return "LOCAL_" + "".join(char if char.isalnum() else "_" for char in stem)[:40]


def _project_id_for(candidate):
    context = candidate.get("project_context")
    if not isinstance(context, dict):
        return None
    project_id = context.get("project_id")
    if project_id is None:
        return None
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("M1.5 project_context.project_id 无效")
    return project_id


def _allows_project_continuity(candidate):
    context = candidate.get("project_context")
    if not isinstance(context, dict):
        return False
    name = str(context.get("canonical_name") or "")
    confidence = context.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < 0.85:
        return False
    return (
        ("项目" in name or "系统" in name)
        and not name.endswith("专项")
    )


def _link_task_project(database, task_id, project_id):
    with connect(database) as connection:
        project = connection.execute(
            "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise ValueError("M1.5 project_id 未写入项目主表")
        cursor = connection.execute(
            "UPDATE tasks SET project_id = ? WHERE task_id = ?",
            (project_id, task_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("M6 任务未写入，无法关联项目主表")
        connection.commit()


def _description_for(candidate):
    return "\n".join([
        "部门：" + candidate["department"],
        "项目：" + candidate["project"],
        candidate["description"],
        "优先级：" + candidate["priority"],
    ])


def _create_command(candidate, source):
    return {
        "command_index": 1,
        "db_action": "CREATE",
        "target_task_id": None,
        "expected_version": None,
        "task_patch": empty_patch(
            title=candidate["title"],
            description=_description_for(candidate),
            work_items=candidate["work_items"],
            assignee_raw=candidate["assignee"] or None,
            deadline_raw=candidate["deadline"] or None,
            status=STATUS_MAP.get(candidate["status"], "open"),
        ),
        "evidence": {
            "segment_id": source["segment_id"],
            "subsegment_id": source["subsegment_id"],
            "text": candidate["evidence"],
        },
        "reason_code": "M8_NEW_PROJECT_TASK",
        "ambiguities": [],
    }


def _update_command(candidate, source, match_context):
    target_task_id = match_context["unique_target_task_id"]
    target = next(
        item
        for item in match_context["candidates"]
        if item["task_id"] == target_task_id
    )
    candidate_status = STATUS_MAP.get(candidate["status"], "open")
    if candidate_status != target["status"]:
        action = "UPDATE_STATUS"
        patch = empty_patch(status=candidate_status)
        reason_code = "M8_PROJECT_TASK_STATUS_UPDATE"
    else:
        action = "UPDATE_FIELDS"
        patch = empty_patch(
            title=candidate["title"],
            description=_description_for(candidate),
            work_items=candidate["work_items"],
            assignee_raw=candidate["assignee"] or None,
            deadline_raw=candidate["deadline"] or None,
        )
        reason_code = "M8_PROJECT_TASK_PROGRESS_UPDATE"
    return {
        "command_index": 1,
        "db_action": action,
        "target_task_id": target_task_id,
        "expected_version": target["version"],
        "task_patch": patch,
        "evidence": {
            "segment_id": source["segment_id"],
            "subsegment_id": source["subsegment_id"],
            "text": candidate["evidence"],
        },
        "reason_code": reason_code,
        "ambiguities": [],
    }


def _review_command(candidate, source):
    return {
        "command_index": 1,
        "db_action": "NOOP",
        "target_task_id": None,
        "expected_version": None,
        "task_patch": empty_patch(),
        "evidence": {
            "segment_id": source["segment_id"],
            "subsegment_id": source["subsegment_id"],
            "text": candidate["evidence"],
        },
        "reason_code": "M8_AMBIGUOUS_PROJECT_TASK",
        "ambiguities": ["同项目存在相似历史任务，需要人工确认新建或更新"],
    }


def _enqueue_match_review(
    database,
    run_id,
    meeting_id,
    candidate,
    match_context,
):
    identity = "{}\0{}".format(run_id, candidate["candidate_id"])
    candidate_id = "M8" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:16].upper()
    confidence = (
        match_context["candidates"][0]["match_score"]
        if match_context["candidates"]
        else None
    )
    review_payload = {
        "stage": "M8",
        "candidate": candidate,
        "match_context": match_context,
    }
    with connect(database) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO review_candidates (
                candidate_id, run_id, meeting_id, confidence_score,
                confidence_source, reason_code, candidate_json,
                status, created_at
            ) VALUES (?, ?, ?, ?, 'm8_deterministic',
                      'M8_AMBIGUOUS_PROJECT_TASK', ?, 'pending', ?)
            """,
            (
                candidate_id,
                run_id,
                meeting_id,
                confidence,
                json.dumps(review_payload, ensure_ascii=False),
                utc_now(),
            ),
        )
        connection.commit()
    return candidate_id


def import_candidates(payload, database, output, meeting_id=None, run_id=None):
    if payload.get("schema_version") != "m2.task_gate.v2":
        raise ValueError("只接受 m2.task_gate.v2 输出")
    candidates = payload.get("extract_candidates")
    if not isinstance(candidates, list):
        raise ValueError("extract_candidates 必须是数组")
    initialize_database(database)
    meeting_id = meeting_id or meeting_id_for(payload.get("source_file"))
    run_id = run_id or "LOCAL_" + datetime.now(timezone.utc).strftime(
        "%Y%m%d_%H%M%S"
    )
    validator = CommandValidator()
    executor = CommandExecutor(database)
    matcher = ProjectTaskMatcher(database)
    project_ids = [_project_id_for(candidate) for candidate in candidates]
    project_candidate_counts = Counter(
        project_id for project_id in project_ids if project_id
    )
    results = []
    match_decisions = []
    project_linked_count = 0
    m8_review_count = 0
    for candidate, project_id in zip(candidates, project_ids):
        segment_id = candidate["candidate_id"]
        source = {
            "segment_id": segment_id,
            "subsegment_id": "001",
            "text": candidate["source_text"],
        }
        allow_project_continuity = (
            project_id is not None
            and project_candidate_counts[project_id] == 1
            and _allows_project_continuity(candidate)
        )
        match_context = matcher.match(
            candidate,
            project_id,
            current_meeting_id=meeting_id,
            allow_project_continuity=allow_project_continuity,
        )
        decision = match_context["decision"]
        if decision == "UPDATE":
            command = _update_command(candidate, source, match_context)
        elif decision == "REVIEW":
            command = _review_command(candidate, source)
            review_candidate_id = _enqueue_match_review(
                database,
                run_id,
                meeting_id,
                candidate,
                match_context,
            )
            m8_review_count += 1
        else:
            command = _create_command(candidate, source)
            review_candidate_id = None
        batch = {
            "schema_version": "m6_db_command_v1",
            "source_key": "{}:{}:001".format(meeting_id, segment_id),
            "commands": [command],
        }
        validator.validate_batch(batch, source, meeting_id, match_context)
        command_results = executor.execute_batch(batch, run_id, meeting_id)
        match_decisions.append({
            "candidate_id": candidate["candidate_id"],
            "project_id": project_id,
            "decision": decision,
            "match_mode": match_context["match_mode"],
            "unique_target_task_id": match_context[
                "unique_target_task_id"
            ],
            "candidates": match_context["candidates"],
            "review_candidate_id": (
                review_candidate_id if decision == "REVIEW" else None
            ),
        })
        for result in command_results:
            task_id = result.get("task_id")
            if project_id and task_id and result["execution_status"] in {
                "applied", "noop", "duplicate"
            }:
                _link_task_project(database, task_id, project_id)
                project_linked_count += 1
        results.extend(command_results)
    report = {
        "meeting_id": meeting_id,
        "run_id": run_id,
        "m2_extract_count": len(candidates),
        "m2_review_count": len(payload.get("review_candidates", [])),
        "m8_review_count": m8_review_count,
        "project_linked_count": project_linked_count,
        "match_decisions": match_decisions,
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description="M2 项目级候选导入 M6 数据库")
    parser.add_argument("input", type=Path, help="m2.task_gate.v2 JSON")
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "data" / "demo.db",
    )
    parser.add_argument("--output", "-o", type=Path, required=True)
    parser.add_argument("--meeting-id")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    report = import_candidates(
        json.loads(args.input.read_text(encoding="utf-8")),
        args.database,
        args.output,
        args.meeting_id,
        args.run_id,
    )
    print("M6: 已处理 {} 条；复核 {} 条".format(
        len(report["results"]), report["m2_review_count"]
    ))


if __name__ == "__main__":
    main()
