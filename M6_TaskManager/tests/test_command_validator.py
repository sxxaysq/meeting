from __future__ import annotations

from src.models import LifecycleDecision

from .helpers import candidate, context, historical_task, item, repository, validator


def decision(action: str, target: str | None = None, **overrides):
    payload = {
        "decision": action,
        "target_task_id": target,
        "reason": "测试理由",
        "event_summary": None,
        "changes": {},
        "department_change": None,
    }
    payload.update(overrides)
    return LifecycleDecision.from_dict(payload)


def test_create_uses_only_m2_fields(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    command = validator(repo).build(context(), decision("CREATE"), [])
    assert command.action.value == "CREATE"
    assert command.changes["title"] == "建设数据中心"
    assert command.changes["project_entity_id"] == "P-HSQ2"


def test_target_must_be_in_candidate_set(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    command = validator(repo).build(
        context(), decision("PROGRESS_UPDATE", "TASK-FORGED"), [candidate(repo)]
    )
    assert command.action.value == "REVIEW"
    assert "候选集合" in command.reason


def test_same_project_does_not_force_update(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    command = validator(repo).build(
        context(
            current_item=item(
                title="建设火灾监测系统",
                content="新增火灾监测系统建设任务。",
            )
        ),
        decision("CREATE"),
        [candidate(repo)],
    )
    assert command.action.value == "CREATE"


def test_department_conflict_without_transfer_is_review(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    source = context(
        current_item=item(department="研发中心"),
    )
    command = validator(repo).build(
        source,
        decision("PROGRESS_UPDATE", "TASK-001"),
        [candidate(repo)],
    )
    assert command.action.value == "REVIEW"
    assert "部门冲突" in command.reason


def test_department_alias_is_not_a_conflict(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    source = context(current_item=item(department="智能矿山部"))
    command = validator(repo).build(
        source,
        decision("PROGRESS_UPDATE", "TASK-001"),
        [candidate(repo)],
    )
    assert command.action.value == "PROGRESS_UPDATE"


def test_completed_task_requires_reopen(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task(status="COMPLETED"))
    command = validator(repo).build(
        context(),
        decision("PROGRESS_UPDATE", "TASK-001"),
        [candidate(repo)],
    )
    assert command.action.value == "REVIEW"


def test_transfer_requires_explicit_grounded_evidence(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    content = "该数据中心建设任务后续移交研发中心负责。"
    source = context(
        current_item=item(content=content, department="研发中心")
    )
    command = validator(repo).build(
        source,
        decision(
            "TRANSFER",
            "TASK-001",
            department_change={
                "from_department": "智能矿山事业部",
                "to_department": "研发中心",
                "evidence": content,
            },
        ),
        [candidate(repo)],
    )
    assert command.action.value == "TRANSFER"
    assert command.changes["department_id"] == "D-RD"


def test_hallucinated_modify_is_review(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    command = validator(repo).build(
        context(),
        decision(
            "MODIFY",
            "TASK-001",
            changes={"title": "会议没有出现的新目标"},
        ),
        [candidate(repo)],
    )
    assert command.action.value == "REVIEW"
    assert "缺少当前 Item 依据" in command.reason
