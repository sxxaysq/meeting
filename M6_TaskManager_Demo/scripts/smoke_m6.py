"""不调用远端模型的 M6→SQLite 固定样例 smoke。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.database import initialize_database
from app.m6.executor import CommandExecutor
from app.m6.model_client import RuleBasedM6Client
from app.m6.service import TaskAutomationService
from app.m6.target_matcher import TargetMatcher
from app.m6.validator import CommandValidator
from app.task_repository import TaskRepository


def record(
    text: str,
    category: str,
    segment_id: str,
    actor: str | None,
) -> dict:
    missing = ["start_ms", "end_ms", "asr_confidence", "nbest"]
    if actor is None:
        missing.append("speaker_id")
    return {
        "segment_id": segment_id,
        "subsegment_id": "001",
        "speaker_id": actor,
        "speaker_confidence": 0.9 if actor else 0.0,
        "start_ms": None,
        "end_ms": None,
        "text_raw": text,
        "asr_confidence": None,
        "nbest": [],
        "missing_fields": missing,
        "task_category": category,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("demo_runs/smoke_m6"),
    )
    args = parser.parse_args()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    database_path = work_dir / "smoke.db"
    initialize_database(database_path, seed_demo_data=True)
    repository = TaskRepository(database_path)
    service = TaskAutomationService(
        repository=repository,
        client=RuleBasedM6Client(),
        matcher=TargetMatcher(),
        validator=CommandValidator(),
        executor=CommandExecutor(database_path),
    )
    records = [
        record(
            "通风队下周一前提交新风量测定方案。",
            "task",
            "seg_new",
            "通风队",
        ),
        record(
            "原定周五提交3125设备应急方案延期到月底。",
            "task_update",
            "seg_update",
            "机电队",
        ),
        record(
            "完成主井设备检查这项任务已经全部完成。",
            "task_update",
            "seg_complete",
            "运输队",
        ),
        record(
            "整理上月培训台账这项任务取消，不再执行。",
            "task_update",
            "seg_delete",
            "培训科",
        ),
        record(
            "本月原煤产量完成计划的95%。",
            "report",
            "seg_report",
            None,
        ),
    ]
    summary = service.process_records(
        records,
        run_id="SMOKE_M6",
        meeting={
            "meeting_id": "DEMO_SMOKE_M6",
            "meeting_date": "2026-07-27",
            "meeting_type": "调度会",
        },
        output_dir=work_dir / "m6",
    )
    payload = {
        "summary": summary,
        "tasks": repository.list_tasks(include_deleted=True),
        "events": repository.list_events(run_id="SMOKE_M6"),
    }
    output = work_dir / "result.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    if summary["applied_count"] != 4 or summary["failed_count"] != 0:
        raise SystemExit("M6 smoke 未达到预期")


if __name__ == "__main__":
    main()

