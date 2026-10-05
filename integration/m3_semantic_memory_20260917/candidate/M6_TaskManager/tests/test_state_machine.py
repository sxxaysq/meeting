from __future__ import annotations

import pytest

from src.models import DecisionValidationError
from src.state_machine import next_status


@pytest.mark.parametrize(
    ("status", "action", "expected"),
    [
        ("OPEN", "PROGRESS_UPDATE", "IN_PROGRESS"),
        ("IN_PROGRESS", "COMPLETE", "COMPLETED"),
        ("BLOCKED", "CANCEL", "CANCELLED"),
        ("COMPLETED", "REOPEN", "IN_PROGRESS"),
        ("CANCELLED", "REOPEN", "IN_PROGRESS"),
    ],
)
def test_valid_transitions(status, action, expected) -> None:
    assert next_status(status, action).value == expected


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("COMPLETED", "PROGRESS_UPDATE"),
        ("OPEN", "REOPEN"),
        ("CANCELLED", "COMPLETE"),
    ],
)
def test_invalid_transitions(status, action) -> None:
    with pytest.raises(DecisionValidationError):
        next_status(status, action)
