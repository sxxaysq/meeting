"""Resolve validated department names to deterministic dispatch routes."""

from __future__ import annotations

from typing import Any

from .candidate_retriever import normalize_text
from .repository import TaskRepository


class DepartmentRouter:
    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

    def resolve(self, name: str | None) -> dict[str, Any] | None:
        normalized = normalize_text(name)
        if not normalized:
            return None
        matches = []
        for department in self.repository.list_departments():
            names = [department["name"], *department.get("aliases", [])]
            if normalized in {normalize_text(value) for value in names}:
                matches.append(department)
        if len(matches) != 1:
            return None
        return matches[0]
