"""Shared M6 domain types and invariants."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class LifecycleAction(StrEnum):
    CREATE = "CREATE"
    PROGRESS_UPDATE = "PROGRESS_UPDATE"
    MODIFY = "MODIFY"
    COMPLETE = "COMPLETE"
    CANCEL = "CANCEL"
    REOPEN = "REOPEN"
    TRANSFER = "TRANSFER"
    REVIEW = "REVIEW"
    SKIP = "SKIP"
    ROUTE_M4 = "ROUTE_M4"
    MULTI = "MULTI"


class TaskStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


TASK_ITEM_TYPES = {
    "PROJECT_TASK",
    "RESEARCH_TASK",
    "NON_PROJECT_WORK",
}
ALL_ITEM_TYPES = TASK_ITEM_TYPES | {"NON_TASK_ITEM"}


class M6Error(RuntimeError):
    """Base exception for expected M6 failures."""


class InputContractError(M6Error):
    """M1 input is unsafe or does not satisfy the published contract."""


class DecisionValidationError(M6Error):
    """An LLM decision violates a deterministic safety boundary."""


class ExecutionConflict(M6Error):
    """A task changed after the lifecycle decision was produced."""


class BusinessAmbiguity(M6Error):
    """Facts are valid but the requested business operation remains ambiguous."""


class TechnicalFailure(M6Error):
    """Retryable model/format failure, never a business review."""


@dataclass(frozen=True)
class SourceContext:
    source_document_id: str
    source_item_id: str
    source_mode: str
    item_index: int
    item: dict[str, Any]
    source_trace: dict[str, Any]
    project_entity_id: str | None
    input_validation: dict[str, Any]
    admission: dict[str, Any] | None = None
    input_stage: str = 'M1'
    origin_document_id: str | None = None

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "source_mode": self.source_mode,
            "item_index": self.item_index,
            "source_trace": self.source_trace,
            "input_validation": self.input_validation,
            'input_stage': self.input_stage,
            'origin_document_id': self.origin_document_id or self.source_document_id,
            **({'admission': self.admission} if self.admission else {}),
        }


@dataclass(frozen=True)
class LifecycleDecision:
    decision: LifecycleAction
    target_task_id: str | None
    reason: str
    event_summary: str | None
    changes: dict[str, Any]
    department_change: dict[str, Any] | None
    scope: str = 'same_task'
    completion_evidence: str | None = None
    expand: bool = False
    # MULTI only: one item carries several independent goals, each mapping to its
    # own validated sub-decision. Never nested (schema forbids MULTI inside).
    sub_decisions: tuple["LifecycleDecision", ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LifecycleDecision":
        return cls(
            decision=LifecycleAction(payload["decision"]),
            target_task_id=payload.get("target_task_id"),
            reason=str(payload.get("reason") or "").strip(),
            event_summary=payload.get("event_summary"),
            changes=dict(payload.get("changes") or {}),
            department_change=payload.get("department_change"),
            scope=payload.get('scope','same_task'),
            completion_evidence=payload.get('completion_evidence'),
            expand=payload.get('expand',False),
            sub_decisions=tuple(
                cls.from_dict(sub) for sub in payload.get('sub_decisions') or ()
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "target_task_id": self.target_task_id,
            "reason": self.reason,
            "event_summary": self.event_summary,
            "changes": self.changes,
            "department_change": self.department_change,
            "scope": self.scope,
            "completion_evidence": self.completion_evidence,
            "expand": self.expand,
            "sub_decisions": [sub.as_dict() for sub in self.sub_decisions],
        }


@dataclass(frozen=True)
class TaskCommand:
    action: LifecycleAction
    target_task_id: str | None
    expected_version: int | None
    source_document_id: str
    source_item_id: str
    event_content: str
    reason: str
    changes: dict[str, Any]
    department_change: dict[str, Any] | None
    provenance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "m6.task_command.v1",
            "action": self.action.value,
            "target_task_id": self.target_task_id,
            "expected_version": self.expected_version,
            "source_document_id": self.source_document_id,
            "source_item_id": self.source_item_id,
            "event_content": self.event_content,
            "reason": self.reason,
            "changes": self.changes,
            "department_change": self.department_change,
            "provenance": self.provenance,
        }
