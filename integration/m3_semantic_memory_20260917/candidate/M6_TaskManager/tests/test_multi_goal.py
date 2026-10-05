"""Coverage for the two review-noise fixes.

1a. A NON_PROJECT_WORK item (company-level directive) legitimately has
    department=null; CREATE falls back to the configured default Department
    Master entry and marks the change, instead of forcing a BUSINESS_REVIEW.
2a. A multi-goal item whose goals each map to a unique candidate (or a clear
    new CREATE) is judged MULTI and executed as validated sub-commands in one
    atomic transaction, instead of a forced REVIEW.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.executor import TaskExecutor
from src.lifecycle_judge import LifecycleJudge
from src.llm_client import ScriptedLifecycleClient
from src.models import DecisionValidationError, LifecycleAction, LifecycleDecision

from .helpers import candidate, context, historical_task, item, repository, validator
from .test_command_validator import decision
from .test_service import service

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "lifecycle_decision.schema.json")
    .read_text(encoding="utf-8")
)


def multi_decision(*subs: LifecycleDecision) -> LifecycleDecision:
    return LifecycleDecision(
        decision=LifecycleAction.MULTI,
        target_task_id=None,
        reason="多目标拆分",
        event_summary=None,
        changes={},
        department_change=None,
        sub_decisions=tuple(subs),
    )


def multi_payload(**overrides):
    payload = {
        "decision": "MULTI",
        "target_index": None,
        "fields": [],
        "scope": "same_task",
        "reason": "两个独立目标分别归属",
        "evidence": None,
        "sub_decisions": [
            {"decision": "PROGRESS_UPDATE", "target_index": 0, "fields": [],
             "scope": "same_task", "reason": "目标一是候选0的后续", "evidence": None},
            {"decision": "CREATE", "target_index": None, "fields": [],
             "scope": "same_task", "reason": "目标二是新目标", "evidence": None},
        ],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- 1a: default department


def test_non_project_work_without_department_uses_default_route(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.upsert_department("D-CO", "公司级", "department://公司级/tasks")
    source = context(current_item=item(
        item_type="NON_PROJECT_WORK", department=None, project=None,
        title="统筹收入回款", content="各部门要统筹做好收入、回款等工作安排。"))
    command = validator(repo).build(source, decision("CREATE"), [])
    assert command.action.value == "CREATE"
    assert command.changes["department"] == "公司级"
    assert command.changes["department_fallback"] == "公司级"


def test_project_task_without_department_still_reviews(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.upsert_department("D-CO", "公司级", "department://公司级/tasks")
    source = context(current_item=item(department=None))
    command = validator(repo).build(source, decision("CREATE"), [])
    assert command.action.value == "REVIEW"
    assert "部门无法唯一映射" in command.reason


def test_default_department_is_never_invented(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")  # 主表没有「公司级」
    source = context(current_item=item(
        item_type="NON_PROJECT_WORK", department=None, project=None,
        title="统筹收入回款", content="各部门要统筹做好收入、回款等工作安排。"))
    command = validator(repo).build(source, decision("CREATE"), [])
    assert command.action.value == "REVIEW"
    assert "部门无法唯一映射" in command.reason


# ---------------------------------------------------------------- 2a: schema


def test_multi_payload_passes_schema() -> None:
    assert not list(Draft202012Validator(SCHEMA).iter_errors(multi_payload()))


def test_multi_requires_sub_decisions() -> None:
    bad = multi_payload()
    del bad["sub_decisions"]
    assert list(Draft202012Validator(SCHEMA).iter_errors(bad))


def test_sub_decisions_forbidden_without_multi() -> None:
    bad = multi_payload(decision="PROGRESS_UPDATE", target_index=0)
    assert list(Draft202012Validator(SCHEMA).iter_errors(bad))


def test_sub_decision_shape_limits() -> None:
    check = Draft202012Validator(SCHEMA)
    subs = multi_payload()["sub_decisions"]
    assert list(check.iter_errors(multi_payload(sub_decisions=subs[:1])))  # minItems 2
    assert list(check.iter_errors(multi_payload(sub_decisions=subs * 2)))  # maxItems 3
    assert list(check.iter_errors(multi_payload(
        sub_decisions=[{**subs[0], "decision": "REVIEW"}, subs[1]])))
    assert list(check.iter_errors(multi_payload(
        sub_decisions=[{**subs[0], "decision": "MULTI"}, subs[1]])))
    assert list(check.iter_errors(multi_payload(
        sub_decisions=[{**subs[1], "target_index": 0}, subs[0]])))  # CREATE 带目标


# ---------------------------------------------------------------- 2a: judge parsing


def test_judge_parses_multi_decision(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    cand = candidate(repo)
    judge = LifecycleJudge(ScriptedLifecycleClient([multi_payload()]))
    result, _ = judge.judge(context(), [cand])
    assert result.decision is LifecycleAction.MULTI
    assert result.target_task_id is None
    assert len(result.sub_decisions) == 2
    first, second = result.sub_decisions
    assert first.decision is LifecycleAction.PROGRESS_UPDATE
    assert first.target_task_id == "TASK-001"
    assert second.decision is LifecycleAction.CREATE
    assert second.target_task_id is None
    assert result.as_dict()["sub_decisions"][0]["decision"] == "PROGRESS_UPDATE"


def test_judge_repairs_multi_with_forged_target(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    cand = candidate(repo)
    judge = LifecycleJudge(ScriptedLifecycleClient([
        multi_payload(sub_decisions=[  # 第一次：target_index=9 越界 → 触发修复
            {"decision": "PROGRESS_UPDATE", "target_index": 9, "fields": [],
             "scope": "same_task", "reason": "越界目标", "evidence": None},
            {"decision": "CREATE", "target_index": None, "fields": [],
             "scope": "same_task", "reason": "新目标", "evidence": None},
        ]),
        multi_payload(),  # 修复后合法
    ]))
    result, audit = judge.judge(context(), [cand])
    assert result.decision is LifecycleAction.MULTI
    assert len(result.sub_decisions) == 2
    assert audit["format_retries"] == 1


# ---------------------------------------------------------------- 2a: validator


def test_build_multi_stamps_provenance(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    cand = candidate(repo)
    source = context(current_item=item(
        title="企业展厅设计", content="推进企业展厅设计深化与施工配合。"))
    multi = multi_decision(decision("PROGRESS_UPDATE", "TASK-001"), decision("CREATE"))
    commands = validator(repo).build_multi(source, multi, [cand])
    assert [c.action.value for c in commands] == ["PROGRESS_UPDATE", "CREATE"]
    assert commands[0].provenance["multi_goal_index"] == 0
    assert commands[1].provenance["multi_goal_total"] == 2


def test_build_multi_collapses_on_business_ambiguity(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    cand = candidate(repo)
    source = context(current_item=item(
        department=None, title="企业展厅设计", content="推进企业展厅设计深化与施工配合。"))
    multi = multi_decision(decision("PROGRESS_UPDATE", "TASK-001"), decision("CREATE"))
    commands = validator(repo).build_multi(source, multi, [cand])
    assert len(commands) == 1
    assert commands[0].action.value == "REVIEW"
    assert commands[0].reason.startswith("BUSINESS_REVIEW: 部门无法唯一映射")


def test_build_multi_rejects_structural_violation(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    multi = multi_decision(decision("PROGRESS_UPDATE", "TASK-FORGED"), decision("CREATE"))
    with pytest.raises(DecisionValidationError):
        validator(repo).build_multi(context(), multi, [])


def test_build_multi_requires_two_subs(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    multi = multi_decision(decision("CREATE"))
    with pytest.raises(DecisionValidationError):
        validator(repo).build_multi(context(), multi, [])


# ---------------------------------------------------------------- 2a: executor & service


def test_execute_multi_applies_all_and_replays(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    cand = candidate(repo)
    source = context(current_item=item(
        title="企业展厅设计", content="推进企业展厅设计深化与施工配合。"))
    multi = multi_decision(decision("PROGRESS_UPDATE", "TASK-001"), decision("CREATE"))
    commands = validator(repo).build_multi(source, multi, [cand])
    executor = TaskExecutor(repo)
    result = executor.execute_multi(commands)
    assert result["execution_status"] == "APPLIED"
    assert result["action"] == "MULTI"
    assert [r["action"] for r in result["sub_results"]] == ["PROGRESS_UPDATE", "CREATE"]
    assert len(repo.list_tasks()) == 2
    assert len(repo.list_events()) == 2
    assert repo.get_task('TASK-001')['description'].endswith(source.item['content'])
    replay = executor.execute_multi(commands)
    assert replay["execution_status"] == "DUPLICATE"
    assert len(repo.list_tasks()) == 2


def test_service_processes_multi_item(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task())
    source = context(current_item=item(
        title="企业展厅设计", content="推进企业展厅设计深化与施工配合。"))
    workflow = service(repo, [multi_payload()])
    result = workflow.process_context(source)
    assert result["execution"]["action"] == "MULTI"
    assert result["execution"]["execution_status"] == "APPLIED"
    assert [r["action"] for r in result["execution"]["sub_results"]] == ["PROGRESS_UPDATE", "CREATE"]
    assert result["decision"]["decision"] == "MULTI"
    assert len(repo.list_tasks()) == 2
    replay = workflow.process_context(source)
    assert replay["idempotent_replay"] is True
    assert len(repo.list_tasks()) == 2
