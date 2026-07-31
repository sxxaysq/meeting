"""根据已保存事件和验证失败重新核对运行统计。"""

from __future__ import annotations

import argparse
import json

from app.config import Settings
from app.task_repository import TaskRepository


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    args = parser.parse_args()
    settings = Settings.load()
    repository = TaskRepository(settings.database_path)
    run = repository.get_run(args.run_id)
    if run is None:
        raise SystemExit(f"运行不存在：{args.run_id}")
    events = repository.list_events(run_id=args.run_id)
    applied = sum(
        event["execution_status"] == "applied" for event in events
    )
    noop = sum(
        event["execution_status"] in {"noop", "duplicate"}
        for event in events
    )
    failed = sum(
        event["execution_status"] == "failed" for event in events
    )
    summary = dict(run.get("summary") or {})
    summary.update(
        {
            "command_count": applied + noop + failed,
            "applied_count": applied,
            "noop_count": noop,
            "failed_count": failed,
        }
    )
    repository.update_run(
        args.run_id,
        command_count=summary["command_count"],
        applied_count=applied,
        noop_count=noop,
        failed_count=failed,
        summary_json=json.dumps(summary, ensure_ascii=False),
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

