from __future__ import annotations

import sqlite3

import pytest

from src.executor import TaskExecutor
from src.models import LifecycleDecision, TaskCommand

from .helpers import candidate, context, historical_task, repository, validator


def lifecycle(action: str, target: str | None = None) -> LifecycleDecision:
    return LifecycleDecision.from_dict(
        {
            "decision": action,
            "target_task_id": target,
            "reason": "测试执行",
            "event_summary": None,
            "changes": {},
            "department_change": None,
        }
    )


def test_create_is_transactional_and_idempotent(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    source = context()
    command = validator(repo).build(source, lifecycle("CREATE"), [])
    executor = TaskExecutor(repo)
    first = executor.execute(command)
    second = executor.execute(command)
    assert first["execution_status"] == "APPLIED"
    assert second["execution_status"] == "DUPLICATE"
    assert len(repo.list_tasks()) == 1
    assert len(repo.list_events(first["task_id"])) == 1
    assert len(repo.list_dispatches()) == 1


def test_progress_adds_event_without_overwriting_task(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    before = repo.get_task("TASK-001")
    source = context(source_item_id="ITEM:PROGRESS")
    command = validator(repo).build(
        source,
        lifecycle("PROGRESS_UPDATE", "TASK-001"),
        [candidate(repo)],
    )
    result = TaskExecutor(repo).execute(command)
    after = repo.get_task("TASK-001")
    assert result["execution_status"] == "APPLIED"
    assert after['version']==before['version']+1
    assert {k:v for k,v in before.items() if k not in ('version','updated_at')}=={k:v for k,v in after.items() if k not in ('version','updated_at')}
    assert len(repo.list_events("TASK-001")) == 1


def test_transaction_rolls_back_if_dispatch_contract_is_broken(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    source = context(source_item_id="ITEM:BROKEN")
    valid = validator(repo).build(source, lifecycle("CREATE"), [])
    changes = dict(valid.changes)
    changes["department_id"] = "D-NOT-EXIST"
    broken = TaskCommand(
        action=valid.action,
        target_task_id=None,
        expected_version=None,
        source_document_id=valid.source_document_id,
        source_item_id=valid.source_item_id,
        event_content=valid.event_content,
        reason=valid.reason,
        changes=changes,
        department_change=None,
        provenance=valid.provenance,
    )
    with pytest.raises(sqlite3.IntegrityError):
        TaskExecutor(repo).execute(broken)
    assert repo.list_tasks() == []
    assert repo.list_events() == []
    assert repo.list_dispatches() == []
