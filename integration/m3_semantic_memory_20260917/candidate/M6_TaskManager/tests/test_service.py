from __future__ import annotations

from src.candidate_retriever import CandidateRetriever
from src.command_validator import CommandValidator
from src.department_router import DepartmentRouter
from src.executor import TaskExecutor
from src.lifecycle_judge import LifecycleJudge
from src.llm_client import ScriptedLifecycleClient
from src.service import TaskLifecycleService

from .helpers import context, item, repository


def service(repo, decisions):
    router = DepartmentRouter(repo)
    return TaskLifecycleService(
        repository=repo,
        retriever=CandidateRetriever(repo),
        judge=LifecycleJudge(ScriptedLifecycleClient(decisions)),
        validator=CommandValidator(repo, router),
        executor=TaskExecutor(repo),
    )


def test_non_task_skips_without_calling_llm(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    result = service(repo, []).process_context(
        context(
            current_item=item(
                item_type="NON_TASK_ITEM",
                title="暂无",
                content="暂无。",
            ),
            project_entity_id=None,
        )
    )
    assert result["execution"]["action"] == "SKIP"
    assert result["llm_called"] is False
    assert repo.list_tasks() == []


def test_duplicate_source_does_not_call_llm_again(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    current = context()
    first_service = service(
        repo,
        [
            {
                "decision": "CREATE",
                "target_index": None,
                "reason": "新任务",
                "evidence": None,
                "fields": [],
                "scope": "same_task",
            }
        ],
    )
    first = first_service.process_context(current)
    second = service(repo, []).process_context(current)
    assert first["execution"]["execution_status"] == "APPLIED"
    assert second["execution"]["execution_status"] == "DUPLICATE"
    assert second["llm_called"] is False
    assert len(repo.list_tasks()) == 1


def test_invalid_model_format_is_not_business_review(tmp_path) -> None:
    repo = repository(tmp_path / "tasks.db")
    result = service(
        repo,
        [
            {
                "decision": "PROGRESS_UPDATE",
                "target_task_id": "TASK-FORGED",
                "reason": "错误目标",
                "event_summary": None,
                "changes": {},
                "department_change": None,
            }
        ],
    ).process_context(context())
    assert result['failure_kind']=='TECHNICAL_FAILURE'
    assert repo.list_reviews()==[]
    assert repo.get_processing_record('DOC-2026-04-14','ITEM:001') is None
