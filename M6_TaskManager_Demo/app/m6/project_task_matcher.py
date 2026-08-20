"""Deterministic history matching for project-level task candidates."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..database import connect


NORMALIZE_PATTERN = re.compile(r"[\W_]+", re.UNICODE)
DEPARTMENT_PATTERN = re.compile(r"(?:^|\n)部门：\s*([^\n]+)")


def _normalize(value: str | None) -> str:
    return NORMALIZE_PATTERN.sub("", str(value or "")).casefold()


def _bigrams(value: str | None) -> set[str]:
    normalized = _normalize(value)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
    }


def _dice(left: str | None, right: str | None) -> float:
    left_bigrams = _bigrams(left)
    right_bigrams = _bigrams(right)
    if not left_bigrams or not right_bigrams:
        return 0.0
    return (
        2.0
        * len(left_bigrams & right_bigrams)
        / (len(left_bigrams) + len(right_bigrams))
    )


def _work_item_score(current: list[str], historical: list[str]) -> float:
    if not current or not historical:
        return 0.0
    best_scores = [
        max(_dice(item, previous) for previous in historical)
        for item in current
    ]
    return sum(best_scores) / len(best_scores)


def _department(task: dict[str, Any]) -> str | None:
    match = DEPARTMENT_PATTERN.search(str(task.get("description") or ""))
    return match.group(1).strip() if match else None


def _task_view(
    task: dict[str, Any],
    score: float,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "title": task["title"],
        "description": task.get("description"),
        "work_items": task.get("work_items", []),
        "assignee_raw": task.get("assignee_raw"),
        "deadline_raw": task.get("deadline_raw"),
        "status": task["status"],
        "version": task["version"],
        "project_id": task.get("project_id"),
        "match_score": round(score, 4),
        "match_reasons": reasons,
    }


class ProjectTaskMatcher:
    """Match a candidate only against active tasks in its resolved project."""

    def __init__(
        self,
        database_path: Path | str,
        same_task_threshold: float = 0.55,
        project_continuity_threshold: float = 0.25,
        review_threshold: float = 0.42,
        uniqueness_gap: float = 0.10,
        max_candidates: int = 5,
    ) -> None:
        self.database_path = Path(database_path)
        self.same_task_threshold = same_task_threshold
        self.project_continuity_threshold = project_continuity_threshold
        self.review_threshold = review_threshold
        self.uniqueness_gap = uniqueness_gap
        self.max_candidates = max_candidates

    def match(
        self,
        candidate: dict[str, Any],
        project_id: str | None,
        current_meeting_id: str | None = None,
        allow_project_continuity: bool = False,
    ) -> dict[str, Any]:
        if not project_id:
            return self._result([], "no_project", "CREATE")

        tasks = self._active_project_tasks(project_id, current_meeting_id)
        if not tasks:
            return self._result([], "no_project_tasks", "CREATE")

        ranked = sorted(
            (
                (*self._score(candidate, task), task)
                for task in tasks
            ),
            key=lambda item: (-item[0], item[2]["task_id"]),
        )
        selected = ranked[: self.max_candidates]
        candidates = [
            _task_view(task, score, reasons)
            for score, reasons, task in selected
        ]
        top_score = selected[0][0]
        second_score = selected[1][0] if len(selected) > 1 else 0.0

        if allow_project_continuity and len(selected) == 1:
            if top_score >= self.project_continuity_threshold:
                candidates[0]["match_reasons"].append(
                    "single_candidate_project_continuity"
                )
                return self._result(
                    candidates,
                    "unique_project_continuity",
                    "UPDATE",
                    selected[0][2]["task_id"],
                )
            return self._result(
                candidates,
                "project_continuity_requires_review",
                "REVIEW",
            )
        if (
            top_score >= self.same_task_threshold
            and top_score - second_score >= self.uniqueness_gap
        ):
            return self._result(
                candidates,
                "unique_project_task",
                "UPDATE",
                selected[0][2]["task_id"],
            )
        if top_score >= self.review_threshold:
            return self._result(candidates, "ambiguous_project_task", "REVIEW")
        return self._result(candidates, "distinct_project_task", "CREATE")

    def _active_project_tasks(
        self,
        project_id: str,
        excluded_meeting_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM tasks
                WHERE project_id = ?
                  AND is_deleted = 0
                  AND status NOT IN ('completed', 'cancelled')
                  AND (? IS NULL OR source_meeting_id != ?)
                ORDER BY updated_at DESC, task_id
                """,
                (
                    project_id,
                    excluded_meeting_id,
                    excluded_meeting_id,
                ),
            ).fetchall()
        tasks = []
        for row in rows:
            task = dict(row)
            try:
                task["work_items"] = json.loads(
                    task.get("work_items_json") or "[]"
                )
            except (TypeError, json.JSONDecodeError):
                task["work_items"] = []
            tasks.append(task)
        return tasks

    @staticmethod
    def _score(
        candidate: dict[str, Any],
        task: dict[str, Any],
    ) -> tuple[float, list[str]]:
        title_score = _dice(candidate.get("title"), task.get("title"))
        description_score = _dice(
            candidate.get("description"),
            task.get("description"),
        )
        work_score = _work_item_score(
            candidate.get("work_items") or [],
            task.get("work_items") or [],
        )
        score = (
            0.55 * title_score
            + 0.35 * work_score
            + 0.10 * description_score
        )
        reasons = [
            f"title_similarity:{title_score:.3f}",
            f"work_item_similarity:{work_score:.3f}",
            f"description_similarity:{description_score:.3f}",
            "same_project",
        ]

        candidate_department = _normalize(candidate.get("department"))
        task_department = _normalize(_department(task))
        if candidate_department and task_department:
            if candidate_department == task_department:
                score += 0.08
                reasons.append("same_department")
            else:
                score -= 0.03
                reasons.append("different_department")

        candidate_assignee = _normalize(candidate.get("assignee"))
        task_assignee = _normalize(task.get("assignee_raw"))
        if candidate_assignee and task_assignee:
            if candidate_assignee == task_assignee:
                score += 0.10
                reasons.append("same_assignee")
            else:
                score -= 0.05
                reasons.append("different_assignee")

        return max(0.0, min(1.0, score)), reasons

    @staticmethod
    def _result(
        candidates: list[dict[str, Any]],
        match_mode: str,
        decision: str,
        unique_target_task_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "candidates": candidates,
            "unique_target_task_id": unique_target_task_id,
            "match_mode": match_mode,
            "decision": decision,
        }
