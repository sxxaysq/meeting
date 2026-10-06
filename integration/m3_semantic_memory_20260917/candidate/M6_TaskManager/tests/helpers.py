from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from src.command_validator import CommandValidator
from src.department_router import DepartmentRouter
from src.models import SourceContext
from src.repository import TaskRepository


def item(
    *,
    item_type: str = "PROJECT_TASK",
    title: str = "建设数据中心",
    content: str = "推进红沙泉二矿数据中心建设。",
    department: str | None = "智能矿山事业部",
    project: str | None = "红沙泉二矿项目",
    assignee: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "department": department,
        "work_section": "项目推进",
        "delivery_group": "新疆交付组",
        "project": project,
        "item_type": item_type,
        "assignee": assignee or [],
        "title": title,
        "content": content,
        "evidence": {
            "text": content,
            "page_start": 1,
            "page_end": 1,
            "start_char": 10,
            "end_char": 10 + len(content),
            "exact_match": True,
        },
    }


def context(
    *,
    current_item: dict[str, Any] | None = None,
    source_item_id: str = "ITEM:001",
    project_entity_id: str | None = "P-HSQ2",
) -> SourceContext:
    current = current_item or item()
    return SourceContext(
        source_document_id="DOC-2026-04-14",
        source_item_id=source_item_id,
        source_mode="block",
        item_index=0,
        item=current,
        source_trace={
            "item_index": 0,
            "source_indexes": [0],
            "source_evidence": [current["evidence"]],
            "project_entity_id": project_entity_id,
        },
        project_entity_id=project_entity_id,

        input_validation={"status": "PASS", "issues": []},
    )


def repository(path: Path) -> TaskRepository:
    repo = TaskRepository(path)
    repo.initialize()
    repo.upsert_department(
        "D-IM",
        "智能矿山事业部",
        "department://智能矿山事业部/tasks",
        ["智能矿山部"],
    )
    repo.upsert_department(
        "D-RD", "研发中心", "department://研发中心/tasks"
    )
    return repo


def historical_task(
    *,
    task_id: str = "TASK-001",
    title: str = "建设红沙泉二矿数据中心",
    description: str = "完成红沙泉二矿数据中心建设。",
    status: str = "IN_PROGRESS",
    project_entity_id: str | None = "P-HSQ2",
    project: str | None = "红沙泉二矿项目",
    department_id: str = "D-IM",
    department: str = "智能矿山事业部",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "item_type": "PROJECT_TASK",
        "project_entity_id": project_entity_id,
        "project": project,
        "department_id": department_id,
        "department": department,
        "work_section": "项目推进",
        "delivery_group": "新疆交付组",
        "title": title,
        "description": description,
        "assignees": [],
        "status": status,
        "version": 1,
    }


def candidate(repo: TaskRepository, task_id: str = "TASK-001") -> dict[str, Any]:
    task = repo.get_task(task_id)
    assert task is not None
    task["recent_events"] = []
    task["retrieval_score"] = 0.8
    task["retrieval_reasons"] = ["test"]
    return task


def validator(repo: TaskRepository, reader: Any | None = None) -> CommandValidator:
    return CommandValidator(repo, DepartmentRouter(repo), reader)


# ---------------------------------------------------------------- test doubles

COMPLETION_PROVEN = {
    "definite": True,
    "future_or_partial": False,
    "whole_goal": True,
    "phases_covered": True,
    "goal_in_evidence": True,
}
COMPLETION_UNPROVEN = {
    "definite": False,
    "future_or_partial": True,
    "whole_goal": False,
    "phases_covered": False,
    "goal_in_evidence": False,
}
STATUS_EXPLICIT = {"explicit": True, "negated": False}
STATUS_NEGATED = {"explicit": False, "negated": True}
RENAME_YES = {"explicit_rename": True}
RENAME_NO = {"explicit_rename": False}


class ScriptedReader:
    """Stands in for ``SemanticReader``.

    The lifecycle safety properties used to be guaranteed by regex inside the
    validator. They are now guaranteed by a model reading, so a test has to say
    what that reading returned and then assert the validator honoured it. The
    reading itself is covered by ``M3_KnowledgeGraph/tests/test_semantic_layer.py``.
    """

    def __init__(
        self,
        *,
        completion: dict[str, bool] | None = None,
        status: dict[str, bool] | None = None,
        rename: dict[str, bool] | None = None,
    ) -> None:
        self.completion = dict(completion or COMPLETION_PROVEN)
        self.status = dict(status or STATUS_EXPLICIT)
        self.rename = dict(rename or RENAME_NO)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def read_completion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("COMPLETION", kwargs))
        return {**self.completion, "reason": "scripted", "cached": False}

    def read_status_change(self, kind: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((kind, kwargs))
        return {**self.status, "reason": "scripted", "cached": False}

    def read_rename(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("RENAME", kwargs))
        return {**self.rename, "reason": "scripted", "cached": False}

    def stats(self) -> dict[str, int]:
        return {"model_calls": len(self.calls)}


class HashEmbedder:
    """Deterministic offline stand-in for the bge-m3 service.

    Character bigrams hashed into a fixed-width unit vector. Texts that share
    characters land close together, which is enough to exercise ranking and
    thresholds without a GPU service, and reproducible across runs.
    """

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions
        self.calls = 0
        self.texts_embedded = 0
        self.cache_hits = 0
        self.rerank_calls = 0

    @staticmethod
    def _terms(text: str) -> list[str]:
        value = "".join(ch for ch in str(text or "") if not ch.isspace()).casefold()
        if len(value) < 2:
            return [value] if value else []
        return [value[i : i + 2] for i in range(len(value) - 1)]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for term in self._terms(text):
            digest = hashlib.sha256(term.encode("utf-8")).digest()
            slot = digest[0] % self.dimensions
            sign = 1.0 if digest[1] % 2 else -1.0
            vector[slot] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_one(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts_embedded += len(texts)
        return [self._vector(text) for text in texts]

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        self.rerank_calls += 1
        query_vector = self._vector(query)
        scored = [
            (index, self._cosine(query_vector, self._vector(document)))
            for index, document in enumerate(documents)
        ]
        scored.sort(key=lambda pair: -pair[1])
        return scored

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        dot = sum(a * b for a, b in zip(left, right))
        norm_left = math.sqrt(sum(a * a for a in left))
        norm_right = math.sqrt(sum(b * b for b in right))
        if norm_left <= 0.0 or norm_right <= 0.0:
            return 0.0
        return dot / (norm_left * norm_right)

    def stats(self) -> dict[str, int]:
        return {
            "embedding_calls": self.calls,
            "texts_embedded": self.texts_embedded,
            "cache_hits": self.cache_hits,
            "cache_size": 0,
            "rerank_calls": self.rerank_calls,
        }

    def close(self) -> None:
        return None


def family_lookup(mapping: dict[str, str]) -> Any:
    """Build a ``family_lookup`` callable from an explicit name→family mapping."""

    def lookup(name: str) -> str | None:
        return mapping.get(str(name or "").strip())

    return lookup
