from __future__ import annotations

from pathlib import Path
from typing import Any

from src.command_validator import CommandValidator
from src.department_router import DepartmentRouter
from src.models import SourceContext
from src.repository import TaskRepository


def item(
    *,
    item_type: str = "PROJECT_TASK",
    title: str = "建设数据中心",
    content: str = "推进红沙泉二矿数据中心建设。",
    department: str | None = "智能矿山事业部",
    project: str | None = "红沙泉二矿项目",
    assignee: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "department": department,
        "work_section": "项目推进",
        "delivery_group": "新疆交付组",
        "project": project,
        "item_type": item_type,
        "assignee": assignee or [],
        "title": title,
        "content": content,
        "evidence": {
            "text": content,
            "page_start": 1,
            "page_end": 1,
            "start_char": 10,
            "end_char": 10 + len(content),
            "exact_match": True,
        },
    }


def context(
    *,
    current_item: dict[str, Any] | None = None,
    source_item_id: str = "ITEM:001",
    project_entity_id: str | None = "P-HSQ2",
) -> SourceContext:
    current = current_item or item()
    return SourceContext(
        source_document_id="DOC-2026-04-14",
        source_item_id=source_item_id,
        source_mode="block",
        item_index=0,
        item=current,
        merge_trace={
            "item_index": 0,
            "source_indexes": [0],
            "merged": False,
            "evidence_mode": "single",
            "evidence_contiguous": True,
            "source_evidence": [current["evidence"]],
            "project_entity_id": project_entity_id,
        },
        project_entity_id=project_entity_id,
        project_entities=[],
        m2_validation={"status": "PASS", "issues": []},
    )


def repository(path: Path) -> TaskRepository:
    repo = TaskRepository(path)
    repo.initialize()
    repo.upsert_department(
        "D-IM",
        "智能矿山事业部",
        "department://智能矿山事业部/tasks",
        ["智能矿山部"],
    )
    repo.upsert_department(
        "D-RD", "研发中心", "department://研发中心/tasks"
    )
    return repo


def historical_task(
    *,
    task_id: str = "TASK-001",
    title: str = "建设红沙泉二矿数据中心",
    description: str = "完成红沙泉二矿数据中心建设。",
    status: str = "IN_PROGRESS",
    project_entity_id: str | None = "P-HSQ2",
    project: str | None = "红沙泉二矿项目",
    department_id: str = "D-IM",
    department: str = "智能矿山事业部",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "item_type": "PROJECT_TASK",
        "project_entity_id": project_entity_id,
        "project": project,
        "department_id": department_id,
        "department": department,
        "work_section": "项目推进",
        "delivery_group": "新疆交付组",
        "title": title,
        "description": description,
        "assignees": [],
        "status": status,
        "version": 1,
    }


def candidate(repo: TaskRepository, task_id: str = "TASK-001") -> dict[str, Any]:
    task = repo.get_task(task_id)
    assert task is not None
    task["recent_events"] = []
    task["retrieval_score"] = 0.8
    task["retrieval_reasons"] = ["test"]
    return task


def validator(repo: TaskRepository) -> CommandValidator:
    return CommandValidator(repo, DepartmentRouter(repo))
