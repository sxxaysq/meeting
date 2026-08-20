"""调用内网 Qwen 的 M6→SQLite 两动作 smoke。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.database import initialize_database
from app.m6.executor import CommandExecutor
from app.m6.model_client import OpenAICompatibleM6Client
from app.m6.service import TaskAutomationService
from app.m6.target_matcher import TargetMatcher
from app.m6.validator import CommandValidator
from app.task_repository import TaskRepository


def record(
    text: str,
    category: str,
    segment_id: str,
    actor: str,
) -> dict:
    return {
        "segment_id": segment_id,
        "subsegment_id": "001",
        "speaker_id": actor,
        "speaker_confidence": 0.95,
        "start_ms": None,
        "end_ms": None,
        "text_raw": text,
        "asr_confidence": None,
        "nbest": [],
        "missing_fields": [
            "start_ms",
            "end_ms",
            "asr_confidence",
            "nbest",
        ],
        "task_category": category,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("demo_runs/smoke_live_model"),
    )
    args = parser.parse_args()
    settings = Settings.load()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    database_path = work_dir / "live_model.db"
    initialize_database(database_path, seed_demo_data=True)
    repository = TaskRepository(database_path)
    service = TaskAutomationService(
        repository=repository,
        client=OpenAICompatibleM6Client(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.llm_timeout_seconds,
        ),
        matcher=TargetMatcher(max_candidates=settings.max_task_candidates),
        validator=CommandValidator(),
        executor=CommandExecutor(database_path),
    )
    summary = service.process_records(
        [
            record(
                "通风队下周一前提交新风量测定方案。",
                "task",
                "seg_live_new",
                "通风队",
            ),
            record(
                "原定周五提交3125设备应急方案延期到月底。",
                "task_update",
                "seg_live_update",
                "机电队",
            ),
        ],
        run_id="SMOKE_LIVE_MODEL",
        meeting={
            "meeting_id": "DEMO_SMOKE_LIVE_MODEL",
            "meeting_date": "2026-07-27",
            "meeting_type": "调度会",
        },
        output_dir=work_dir / "m6",
    )
    payload = {
        "summary": summary,
        "tasks": repository.list_tasks(include_deleted=True),
        "events": repository.list_events(run_id="SMOKE_LIVE_MODEL"),
    }
    (work_dir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    if summary["applied_count"] != 2 or summary["failed_count"] != 0:
        raise SystemExit("真实 M6 模型 smoke 未达到预期")


if __name__ == "__main__":
    main()

