from __future__ import annotations

from src.candidate_retriever import CandidateRetriever

from .helpers import context, historical_task, repository


def test_project_scope_keeps_same_project_tasks_distinct(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(historical_task(task_id="TASK-DATA"))
    repo.add_historical_task(
        historical_task(
            task_id="TASK-FIRE",
            title="建设火灾监测系统",
            description="完成火灾监测系统建设。",
        )
    )
    repo.add_historical_task(
        historical_task(
            task_id="TASK-OTHER",
            title="建设其他项目数据中心",
            project_entity_id="P-OTHER",
            project="其他项目",
        )
    )
    candidates = CandidateRetriever(repo, top_k=5).retrieve(context())
    assert {entry["task_id"] for entry in candidates} == {
        "TASK-DATA",
        "TASK-FIRE",
    }


def test_closed_candidates_are_recalled_for_reopen(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    repo.add_historical_task(
        historical_task(task_id="TASK-CLOSED", status="COMPLETED")
    )
    repo.add_historical_task(
        historical_task(task_id="TASK-ACTIVE", title="建设综合管控平台")
    )
    candidates = CandidateRetriever(
        repo, top_k=2, closed_quota=1
    ).retrieve(context())
    assert {entry["task_id"] for entry in candidates} == {
        "TASK-CLOSED",
        "TASK-ACTIVE",
    }
