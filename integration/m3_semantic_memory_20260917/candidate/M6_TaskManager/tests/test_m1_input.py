"""Regression coverage for the native nine-field source boundary."""
import json
from copy import deepcopy

import pytest

from src.m1_input import load_m1_payload
from src.models import InputContractError
from .helpers import item, repository
from .test_service import service


def envelope():
    return {"items": [item()], "mode": "generic", "source_document_id": "native-input"}


def test_source_ids_and_provenance_are_stable(tmp_path):
    path = tmp_path / "input.json"
    payload = envelope()
    path.write_text(json.dumps(payload), encoding="utf-8")
    first_doc, first = load_m1_payload(path)
    second_doc, second = load_m1_payload(path)
    assert first_doc == second_doc == "native-input"
    assert first[0].source_item_id == second[0].source_item_id
    assert first[0].item == payload["items"][0]
    assert first[0].source_trace == {"item_index": 0, "source_indexes": [0],
                                    "source_evidence": [payload["items"][0]["evidence"]]}
    assert first[0].provenance["input_stage"] == "M1"
    changed = deepcopy(payload)
    changed["items"][0]["content"] += " additional source fact"
    path.write_text(json.dumps(changed), encoding="utf-8")
    assert load_m1_payload(path)[1][0].source_item_id != first[0].source_item_id


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(source_trace=[]),
    lambda p: p.update(validation={"status": "PASS"}),
    lambda p: p["items"][0].update(task_id="forged"),
    lambda p: p["items"][0].pop("department"),
    lambda p: p["items"][0]["evidence"].update(text=""),
    lambda p: p["items"][0]["evidence"].update(start_char=999),
    lambda p: p["items"][0]["evidence"].update(page_start=2, page_end=1),
    lambda p: p.update(mode="unsupported"),
    lambda p: p.update(source_document_id=""),
])
def test_invalid_source_is_rejected_before_any_write(tmp_path, mutation):
    payload = envelope()
    mutation(payload)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    repo = repository(tmp_path / "tasks.sqlite")
    with pytest.raises(InputContractError):
        service(repo, []).process_m1_file(path, output_path=tmp_path / "result.json")
    assert repo.list_tasks() == repo.list_reviews() == repo.list_events() == []


def test_document_override_preserves_origin_and_empty_input(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(envelope()), encoding="utf-8")
    doc, contexts = load_m1_payload(path, document_id="replay-input")
    assert doc == "replay-input" and contexts[0].origin_document_id == "native-input"
    path.write_text(json.dumps({"items": [], "source_document_id": "empty"}), encoding="utf-8")
    assert load_m1_payload(path) == ("empty", [])
