"""Lexical candidate recall; scores never decide lifecycle actions."""

from __future__ import annotations

import re
from typing import Any

from .models import SourceContext, TaskStatus
from .repository import TaskRepository


NORMALIZE_PATTERN = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
ACTIVE = {
    TaskStatus.OPEN.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.BLOCKED.value,
}
CLOSED = {
    TaskStatus.COMPLETED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.CLOSED.value,
}


def normalize_text(value: Any) -> str:
    return NORMALIZE_PATTERN.sub("", str(value or "")).casefold()


def bigrams(value: Any) -> set[str]:
    normalized = normalize_text(value)
    if not normalized:
        return set()
    if len(normalized) == 1:
        return {normalized}
    return {
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
    }


def dice(left: Any, right: Any) -> float:
    left_terms = bigrams(left)
    right_terms = bigrams(right)
    if not left_terms or not right_terms:
        return 0.0
    return 2 * len(left_terms & right_terms) / (
        len(left_terms) + len(right_terms)
    )


def _assignee_overlap(current: list[str], historical: list[str]) -> float:
    left = {normalize_text(value) for value in current if normalize_text(value)}
    right = {
        normalize_text(value) for value in historical if normalize_text(value)
    }
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class CandidateRetriever:
    def __init__(
        self,
        repository: TaskRepository,
        *,
        top_k: int = 5,
        closed_quota: int = 2,
        recent_event_limit: int = 5,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        self.repository = repository
        self.top_k = top_k
        self.closed_quota = min(max(0, closed_quota), top_k)
        self.recent_event_limit = recent_event_limit

    def retrieve(self, source: SourceContext) -> list[dict[str, Any]]:
        tasks = self.repository.retrieval_tasks(self.recent_event_limit)
        if not tasks:
            return []
        project_scoped = self._project_scope(source, tasks)
        pool = project_scoped or tasks
        ranked = sorted(
            (self._score(source, task) for task in pool),
            key=lambda entry: (-entry[0], entry[2]["task_id"]),
        )
        active = [entry for entry in ranked if entry[2]["status"] in ACTIVE]
        closed = [entry for entry in ranked if entry[2]["status"] in CLOSED]
        active_limit = self.top_k - self.closed_quota
        if active:
            active_limit = max(1, active_limit)
        selected = active[:active_limit]
        selected.extend(closed[: self.closed_quota])
        if len(selected) < self.top_k:
            chosen_ids = {entry[2]["task_id"] for entry in selected}
            selected.extend(
                entry
                for entry in ranked
                if entry[2]["task_id"] not in chosen_ids
            )
        selected = sorted(
            selected[: self.top_k],
            key=lambda entry: (-entry[0], entry[2]["task_id"]),
        )
        return [self._candidate_view(*entry) for entry in selected]

    @staticmethod
    def _project_scope(
        source: SourceContext,
        tasks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if source.item["item_type"] != "PROJECT_TASK":
            return []
        if source.project_entity_id:
            matched = [
                task
                for task in tasks
                if task.get("project_entity_id") == source.project_entity_id
            ]
            if matched:
                return matched
        project = normalize_text(source.item.get("project"))
        if project:
            matched = [
                task
                for task in tasks
                if normalize_text(task.get("project")) == project
            ]
            if matched:
                return matched
        return []

    @staticmethod
    def _score(
        source: SourceContext,
        task: dict[str, Any],
    ) -> tuple[float, list[str], dict[str, Any]]:
        item = source.item
        event_text = "\n".join(
            event["content"] for event in task.get("recent_events", [])
        )
        title_score = dice(item.get("title"), task.get("title"))
        content_score = dice(item.get("content"), task.get("description"))
        event_score = dice(item.get("content"), event_text)
        assignee_score = _assignee_overlap(
            item.get("assignee") or [], task.get("assignees") or []
        )
        score = (
            0.40 * title_score
            + 0.30 * content_score
            + 0.20 * event_score
            + 0.10 * assignee_score
        )
        reasons = [
            f"title:{title_score:.3f}",
            f"description:{content_score:.3f}",
            f"events:{event_score:.3f}",
            f"assignee:{assignee_score:.3f}",
        ]
        if source.project_entity_id and (
            task.get("project_entity_id") == source.project_entity_id
        ):
            reasons.append("same_project_entity")
        if (
            normalize_text(item.get("department"))
            and normalize_text(item.get("department"))
            == normalize_text(task.get("department"))
        ):
            score += 0.05
            reasons.append("same_department")
        if task["status"] in ACTIVE:
            score += 0.03
            reasons.append("active_priority")
        return min(score, 1.0), reasons, task

    @staticmethod
    def _candidate_view(
        score: float,
        reasons: list[str],
        task: dict[str, Any],
    ) -> dict[str, Any]:
        fields = (
            "task_id",
            "item_type",
            "project_entity_id",
            "project",
            "department_id",
            "department",
            "work_section",
            "delivery_group",
            "title",
            "description",
            "assignees",
            "status",
            "version",
            "created_at",
            "updated_at",
            "recent_events",
        )
        candidate = {field: task.get(field) for field in fields}
        candidate["retrieval_score"] = round(score, 6)
        candidate["retrieval_reasons"] = reasons
        return candidate
