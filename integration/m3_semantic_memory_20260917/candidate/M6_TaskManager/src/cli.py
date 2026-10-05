"""Command-line interface for the M6 lifecycle manager."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .candidate_retriever import CandidateRetriever
from .command_validator import CommandValidator
from .department_router import DepartmentRouter
from .executor import TaskExecutor
from .lifecycle_judge import LifecycleJudge
from .llm_client import OpenAICompatibleLifecycleClient
from .repository import TaskRepository
from .service import TaskLifecycleService


def _repository(path: Path) -> TaskRepository:
    repository = TaskRepository(path)
    repository.initialize()
    return repository


def _load_departments(repository: TaskRepository, path: Path) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Department Master 必须是 JSON 数组")
    for department in payload:
        repository.upsert_department(
            department["department_id"],
            department["name"],
            department["route"],
            department.get("aliases", []),
        )
    return len(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M6 Task Lifecycle Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init-db", help="初始化数据库和部门主数据")
    init.add_argument("--database", type=Path, required=True)
    init.add_argument("--departments", type=Path)

    history = subparsers.add_parser(
        "import-history", help="导入可信历史任务 JSON"
    )
    history.add_argument("input", type=Path)
    history.add_argument("--database", type=Path, required=True)

    process = subparsers.add_parser("process", help="M1→M3检索与项目记忆→M6；硬错误拒绝")
    process.add_argument("input", type=Path)
    process.add_argument("--database", type=Path, required=True)
    process.add_argument("--output", type=Path, required=True)
    process.add_argument("--document-id")
    process.add_argument("--top-k", type=int, default=5)
    process.add_argument("--closed-quota", type=int, default=2)
    process.add_argument(
        "--base-url",
        default=os.getenv("LLM_BASE_URL", "http://192.168.30.215:8000/v1"),
    )
    process.add_argument(
        "--model",
        default=os.getenv("LLM_MODEL", "qwen3.8-27b"),
    )
    process.add_argument(
        "--api-key", default=os.getenv("LLM_API_KEY", "EMPTY")
    )
    process.add_argument(
        "--timeout", type=float, default=float(os.getenv("LLM_TIMEOUT", "180"))
    )

    inspect = subparsers.add_parser("inspect", help="查看数据库摘要")
    inspect.add_argument("--database", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    repository = _repository(args.database)
    if args.command == "init-db":
        count = _load_departments(repository, args.departments) if args.departments else 0
        print(
            json.dumps(
                {"database": str(args.database), "departments": count},
                ensure_ascii=False,
            )
        )
        return
    if args.command == "import-history":
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("历史任务输入必须是 JSON 数组")
        for task in payload:
            repository.add_historical_task(task)
        print(json.dumps({"imported": len(payload)}, ensure_ascii=False))
        return
    if args.command == "inspect":
        print(json.dumps({
            "tasks": repository.list_tasks(),
            "departments": repository.list_departments(),
            "reviews": repository.list_reviews(),
            "dispatches": repository.list_dispatches(),
        }, ensure_ascii=False, indent=2))
        return

    router = DepartmentRouter(repository)
    service = TaskLifecycleService(
        repository=repository,
        retriever=CandidateRetriever(
            repository,
            top_k=args.top_k,
            closed_quota=args.closed_quota,
        ),
        judge=LifecycleJudge(
            OpenAICompatibleLifecycleClient(
                base_url=args.base_url,
                model=args.model,
                api_key=args.api_key,
                timeout_seconds=args.timeout,
            )
        ),
        validator=CommandValidator(repository, router),
        executor=TaskExecutor(repository),
    )
    result = service.process_m1_file(
        args.input,
        output_path=args.output,
        document_id=args.document_id,
    )
    print(json.dumps({
        "source_document_id": result["source_document_id"],
        "item_count": result["item_count"],
        "action_counts": result["action_counts"],
        "execution_status_counts": result["execution_status_counts"],
        "output": str(args.output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
