import json
from dataclasses import replace

import pytest

from src.m1_input import load_m1_payload
from src.models import InputContractError
from src.input_policy import restriction
from .helpers import repository, context, historical_task, candidate, validator, item
from .test_service import service
from .test_command_validator import decision


def test_scoped_input_review_and_replay(tmp_path):
    path = tmp_path / "m1.json"
    path.write_text(json.dumps({"items": [item(), item(item_type="NON_TASK_ITEM")],
                                "source_document_id": "input-policy"}), encoding="utf-8")
    _, contexts = load_m1_payload(path)
    contexts[0].input_validation.update(status="REVIEW", issues=[{
        "code": "SOURCE_IDENTITY_UNCERTAIN", "level": "review", "message": "Needs review",
        "detail": {"item_indexes": [0]}}])
    repo = repository(tmp_path / "tasks.sqlite")
    workflow = service(repo, [])
    first = [workflow.process_context(source) for source in contexts]
    assert [row["execution"]["action"] for row in first] == ["REVIEW", "SKIP"]
    assert all(not row["llm_called"] for row in first)
    assert repo.list_tasks() == [] and len(repo.list_reviews()) == 1
    second = [workflow.process_context(source) for source in contexts]
    assert all(row["execution"]["execution_status"] == "DUPLICATE" for row in second)
    assert len(repo.list_reviews()) == 1


def test_input_review_is_scoped_and_conservative():
    source = context()
    source.input_validation.update(status="REVIEW", issues=[{
        "code": "SOURCE_IDENTITY_UNCERTAIN", "level": "review", "message": "Needs review",
        "detail": {"item_indexes": [1]}}])
    assert restriction(source, "CREATE") is None
    assert restriction(replace(source, item_index=1), "CREATE")
    source.input_validation["issues"][0]["detail"] = {}
    assert "UNSCOPED" in restriction(source, "CREATE")


def test_input_hard_error_rejects_before_write(tmp_path):
    source = context()
    source.input_validation.update(status="ERROR")
    repo = repository(tmp_path / "tasks.sqlite")
    with pytest.raises(InputContractError):
        service(repo, []).process_context(source)
    assert repo.list_tasks() == repo.list_reviews() == repo.list_events() == []


def test_pending_review_blocks_duplicate_create(tmp_path):
    repo=repository(tmp_path/'db.sqlite'); source=context()
    repo.add_historical_task(historical_task())
    source.input_validation.update(status='REVIEW',issues=[{'code':'PROJECT_ENTITY_UNCERTAIN','level':'review',
        'message':'待确认','detail':{'item_indexes':[0]}}])
    service(repo,[]).process_context(source)
    other=context(source_item_id='different-week')
    command=validator(repo).build(other,decision('CREATE'),[])
    assert command.action.value=='REVIEW' and 'PENDING_REVIEW_DUPLICATE' in command.reason
    update=validator(repo).build(other,decision('PROGRESS_UPDATE','TASK-001'),[candidate(repo)])
    assert update.action.value=='PROGRESS_UPDATE'
    other.item['project']='新的规范项目名称'
    update=validator(repo).build(other,decision('PROGRESS_UPDATE','TASK-001'),[candidate(repo)])
    assert update.action.value=='PROGRESS_UPDATE'
