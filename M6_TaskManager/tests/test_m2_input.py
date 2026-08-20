from __future__ import annotations

import json

import pytest

from src.m2_input import load_m2_payload
from src.models import InputContractError

from .helpers import item


def payload(status: str = "PASS") -> dict:
    current = item()
    return {
        "source_mode": "block",
        "items": [current],
        "project_entities": [],
        "merge_trace": [
            {
                "item_index": 0,
                "source_indexes": [0],
                "merged": False,
                "evidence_mode": "single",
                "evidence_contiguous": True,
                "source_evidence": [current["evidence"]],
                "project_entity_id": "P-HSQ2",
                "title_source": "source",
            }
        ],
        "validation": {"status": status, "issues": []},
    }


def test_loads_pass_and_generates_stable_ids(tmp_path) -> None:
    path = tmp_path / "meeting.m2.json"
    path.write_text(json.dumps(payload(), ensure_ascii=False), encoding="utf-8")
    first_document, first = load_m2_payload(path)
    second_document, second = load_m2_payload(path)
    assert first_document == second_document
    assert first[0].source_item_id == second[0].source_item_id
    assert first[0].project_entity_id == "P-HSQ2"


def test_rejects_review_input(tmp_path) -> None:
    path = tmp_path / "review.m2.json"
    path.write_text(
        json.dumps(payload("REVIEW"), ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(InputContractError, match="只接受 M2"):
        load_m2_payload(path)


def test_accepts_review_only_when_explicit_and_preserves_validation(tmp_path) -> None:
    path = tmp_path / "review.m2.json"
    path.write_text(
        json.dumps(payload("REVIEW"), ensure_ascii=False), encoding="utf-8"
    )
    _, contexts = load_m2_payload(path, accept_review=True)
    assert contexts[0].m2_validation["status"] == "REVIEW"


def test_error_input_stays_rejected_even_when_review_is_accepted(tmp_path) -> None:
    path = tmp_path / "error.m2.json"
    path.write_text(
        json.dumps(payload("ERROR"), ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(InputContractError, match="只接受 M2"):
        load_m2_payload(path, accept_review=True)


def test_rejects_missing_trace_coverage(tmp_path) -> None:
    data = payload()
    data["merge_trace"] = []
    path = tmp_path / "bad.m2.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(InputContractError):
        load_m2_payload(path)
