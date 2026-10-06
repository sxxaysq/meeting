"""Retrieve task candidates from historical state, vectors and project families."""

from __future__ import annotations

import re
import threading
from typing import Any, Callable

from M6_TaskManager.src.models import SourceContext, TaskStatus
from M6_TaskManager.src.repository import TaskRepository

from .embedding_client import EmbeddingClient, cosine

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

# Two items are the same goal when their wording is semantically near-identical.
# Calibrated on the real 15-meeting dataset (2077 items, bge-m3, 600
# within-project pairs + 400 cross-project pairs):
#   cross-project  p50 0.507 / p95 0.614 / p99 0.671 / max 0.720, and 0 of 400
#                  pairs reach 0.93;
#   within-project p50 0.756 / p90 0.974 / p99 1.000.
# The 0.80-0.93 band was inspected pair by pair and is made up of the same goal
# restated in a later week ("编制…方案" vs "优化…方案", "跟进…回款" vs
# "完成…30%回款", "跟踪招标结果确认及签订合同" vs "跟踪公示及签订合同"), which is
# exactly what this predicate must catch. 0.88 keeps all of them while sitting
# 0.16 above the cross-project ceiling. Exact normalized equality still
# short-circuits first, so a verbatim repeat never depends on a threshold.
SAME_GOAL_THRESHOLD = 0.88
# Below this a recalled task is not even worth showing the model. Cross-project
# similarity tops out at 0.720, so this floor only trims noise, never a real
# candidate.
RECALL_FLOOR = 0.45

_lock = threading.RLock()
_embedder: EmbeddingClient | None = None
_family_lookup: Callable[[str], str | None] | None = None


def configure_semantics(
    embedder: EmbeddingClient | None = None,
    *,
    family_lookup: Callable[[str], str | None] | None = None,
) -> None:
    """Install the shared embedder and the memory-backed family lookup.

    Without this the module still works, but falls back to exact normalized
    comparison and no semantic recall — which is what the unit tests use.
    """
    global _embedder, _family_lookup
    with _lock:
        if embedder is not None:
            _embedder = embedder
        if family_lookup is not None:
            _family_lookup = family_lookup


def reset_semantics() -> None:
    """Clear the module-level semantic configuration.

    Exists so tests cannot leak an embedder or a family lookup into each other;
    production never calls it.
    """
    global _embedder, _family_lookup
    with _lock:
        _embedder = None
        _family_lookup = None


def embedder() -> EmbeddingClient | None:
    with _lock:
        return _embedder


def normalize_text(value: Any) -> str:
    return NORMALIZE_PATTERN.sub("", str(value or "")).casefold()


def bigrams(value: Any) -> set[str]:
    """Deprecated lexical helper; kept for the pre-semantic eval baseline."""
    normalized = normalize_text(value)
    if not normalized:
        return set()
    if len(normalized) == 1:
        return {normalized}
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def dice(left: Any, right: Any) -> float:
    """Deprecated lexical helper; kept for the pre-semantic eval baseline."""
    left_terms = bigrams(left)
    right_terms = bigrams(right)
    if not left_terms or not right_terms:
        return 0.0
    return 2 * len(left_terms & right_terms) / (len(left_terms) + len(right_terms))


def goal_text(row: dict[str, Any]) -> str:
    """The comparable business goal of an item or a task."""
    title = str(row.get("title") or "")
    body = str(row.get("content") or row.get("description") or "")
    return f"{title}\n{body}".strip()


def family_of(name: Any) -> str | None:
    """Resolved project family from the memory graph, or ``None`` if unknown."""
    text = str(name or "").strip()
    if not text:
        return None
    with _lock:
        lookup = _family_lookup
    if lookup is None:
        return None
    try:
        return lookup(text)
    except Exception:
        # An unreachable memory graph must not silently widen recall; the
        # caller falls back to exact normalized comparison.
        return None


FAMILY_ID_PREFIX = "FAMILY-"


def project_compatible(source: SourceContext, task: dict[str, Any]) -> bool:
    """Check source-backed project scope before considering a historical task."""
    left_key = str(source.project_entity_id or "")
    right_key = str(task.get("project_entity_id") or "")
    if left_key and right_key:
        if left_key == right_key:
            return True
        if left_key.startswith(FAMILY_ID_PREFIX) and right_key.startswith(FAMILY_ID_PREFIX):
            return False
    left_name = str(source.item.get("project") or "")
    right_name = str(task.get("project") or task.get("source_project") or "")
    left_family = family_of(left_name)
    right_family = family_of(right_name)
    if left_family and right_family:
        return left_family == right_family
    return normalize_text(left_name) == normalize_text(right_name)


def same_goal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when both rows state the same business goal.

    Exact normalized equality first (deterministic, threshold-free), then bge-m3
    cosine when an embedder is configured. Never merges on weak similarity.
    """
    left_goal = normalize_text(left.get("content") or left.get("description"))
    right_goal = normalize_text(right.get("content") or right.get("description"))
    if len(left_goal) >= 8 and left_goal == right_goal:
        return True
    left_title = normalize_text(left.get("title"))
    right_title = normalize_text(right.get("title"))
    filler = {"持续推进", "项目推进", "现场跟进", "日常工作"}
    left_key = left_title.replace(normalize_text(left.get("project")), "") if left.get("project") else left_title
    if len(left_key) >= 4 and left_key not in filler and left_title == right_title:
        return True
    client = embedder()
    if client is None:
        return False
    left_text = goal_text(left)
    right_text = goal_text(right)
    if not left_text or not right_text:
        return False
    try:
        vectors = client.embed_many([left_text, right_text])
    except Exception:
        return False
    return cosine(vectors[0], vectors[1]) >= SAME_GOAL_THRESHOLD


def goal_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Cosine between two goals, or 0.0 without an embedder."""
    client = embedder()
    if client is None:
        return 0.0
    left_text = goal_text(left)
    right_text = goal_text(right)
    if not left_text or not right_text:
        return 0.0
    try:
        vectors = client.embed_many([left_text, right_text])
    except Exception:
        return 0.0
    return cosine(vectors[0], vectors[1])


class SemanticTaskIndex:
    """Embeds committed tasks so ``CandidateRetriever`` can recall them.

    Satisfies the ``semantic_search(text, k)`` contract the retriever already
    expected: each hit carries ``task_id``, ``version``, ``last_event_id`` and a
    0..1 ``score``. The retriever discards a hit whose version or last event no
    longer matches the database, so a task updated by another worker thread can
    never contribute stale fields.

    Sync is incremental: only tasks that are new or whose version moved get
    re-embedded, and the embedder caches by text, so steady-state cost per
    retrieve is one embedding of the query.
    """

    def __init__(
        self,
        repository: TaskRepository,
        embedder: EmbeddingClient,
        *,
        recent_event_limit: int = 5,
    ) -> None:
        self.repository = repository
        self.embedder = embedder
        self.recent_event_limit = recent_event_limit
        self._lock = threading.RLock()
        self._vectors: dict[str, list[float]] = {}
        self._state: dict[str, tuple[int, Any]] = {}
        self.syncs = 0
        self.embedded_tasks = 0
        self.searches = 0
        self.stale_hits = 0

    def sync(self) -> int:
        """Embed new or changed tasks; returns how many were (re)embedded."""
        tasks = self.repository.retrieval_tasks(self.recent_event_limit)
        current = {
            task["task_id"]: (
                int(task.get("version") or 0),
                task.get("last_event_id"),
                task,
            )
            for task in tasks
        }
        with self._lock:
            self.syncs += 1
            for task_id in list(self._vectors):
                if task_id not in current:
                    self._vectors.pop(task_id, None)
                    self._state.pop(task_id, None)
            delta = [
                (task_id, row)
                for task_id, row in current.items()
                if self._state.get(task_id) != (row[0], row[1])
            ]
            if not delta:
                return 0
            texts = [self._task_text(row[2]) for _task_id, row in delta]
        vectors = self.embedder.embed_many(texts)
        with self._lock:
            for (task_id, row), vector in zip(delta, vectors):
                self._vectors[task_id] = vector
                self._state[task_id] = (row[0], row[1])
            self.embedded_tasks += len(delta)
        return len(delta)

    @staticmethod
    def _task_text(task: dict[str, Any]) -> str:
        events = "\n".join(
            str(event.get("content") or "")
            for event in (task.get("recent_events") or [])[:5]
        )
        parts = [
            str(task.get("project") or ""),
            str(task.get("title") or ""),
            str(task.get("description") or ""),
            events,
        ]
        return "\n".join(part for part in parts if part.strip())

    def search(self, text: str, k: int = 20) -> list[dict[str, Any]]:
        """Highest-cosine tasks first, as ``semantic_search`` hits."""
        if not str(text or "").strip():
            return []
        self.sync()
        query = self.embedder.embed_one(text)
        with self._lock:
            self.searches += 1
            snapshot = dict(self._vectors)
        scored = [
            (task_id, cosine(query, vector)) for task_id, vector in snapshot.items()
        ]
        scored = [pair for pair in scored if pair[1] >= RECALL_FLOOR]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        hits: list[dict[str, Any]] = []
        for task_id, score in scored[: max(1, int(k))]:
            state = self._state.get(task_id)
            if state is None:
                continue
            hits.append(
                {
                    "task_id": task_id,
                    "version": state[0],
                    "last_event_id": state[1],
                    "score": max(0.0, min(1.0, score)),
                }
            )
        return hits

    def __call__(self, text: str, k: int = 20) -> list[dict[str, Any]]:
        return self.search(text, k)

    def note_stale(self) -> None:
        with self._lock:
            self.stale_hits += 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "indexed_tasks": len(self._vectors),
                "syncs": self.syncs,
                "embedded_tasks": self.embedded_tasks,
                "searches": self.searches,
                "stale_hits": self.stale_hits,
            }


def _assignee_overlap(current: list[str], historical: list[str]) -> float:
    left = {normalize_text(value) for value in current if normalize_text(value)}
    right = {normalize_text(value) for value in historical if normalize_text(value)}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class CandidateRetriever:
    """Ranks historical tasks for one source item."""

    def __init__(
        self,
        repository: TaskRepository,
        *,
        top_k: int = 5,
        closed_quota: int = 2,
        recent_event_limit: int = 5,
        semantic_search: Callable[[str, int], list[dict[str, Any]]] | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        self.repository = repository
        self.top_k = top_k
        self.closed_quota = min(max(0, closed_quota), top_k)
        self.recent_event_limit = recent_event_limit
        self.semantic_search = semantic_search
        self.index_diagnostics: list[str] = []

    def retrieve(self, source: SourceContext, *, expanded: bool = False) -> list[dict[str, Any]]:
        tasks = self.repository.retrieval_tasks(self.recent_event_limit)
        if not tasks:
            return []
        pool = [task for task in tasks if project_compatible(source, task)]
        memory = source.admission or {}
        if str(memory.get("parent_reason", "")).startswith(("保持独立", "保留独立")):
            # Identity is unresolved: also offer same-goal tasks from other
            # families, flagged so the validator reviews instead of writing.
            chosen = {task["task_id"] for task in pool}
            pool += [
                {**task, "identity_uncertain": True}
                for task in tasks
                if task["task_id"] not in chosen and same_goal(source.item, task)
            ]
        semantic = self._semantic_scores(source, pool)
        limit = max(self.top_k, 12) if expanded else self.top_k
        quota = min(4, limit) if expanded else self.closed_quota
        ranked = sorted(
            (
                (score, reasons, task)
                for score, reasons, task in (
                    self._score(source, task, semantic) for task in pool
                )
            ),
            key=lambda entry: (-entry[0], entry[2]["task_id"]),
        )
        active = [entry for entry in ranked if entry[2]["status"] in ACTIVE]
        closed = [entry for entry in ranked if entry[2]["status"] in CLOSED]
        active_limit = limit - quota
        if active:
            active_limit = max(1, active_limit)
        selected = active[:active_limit]
        selected.extend(closed[:quota])
        if len(selected) < limit:
            chosen_ids = {entry[2]["task_id"] for entry in selected}
            selected.extend(
                entry for entry in ranked if entry[2]["task_id"] not in chosen_ids
            )
        selected = sorted(
            selected[:limit], key=lambda entry: (-entry[0], entry[2]["task_id"])
        )
        return [self._candidate_view(*entry) for entry in selected]

    def _semantic_scores(
        self, source: SourceContext, pool: list[dict[str, Any]]
    ) -> dict[str, float]:
        """Cosine per pooled task. A stale or foreign hit is dropped, not trusted."""
        if not self.semantic_search or not pool:
            return {}
        try:
            hits = self.semantic_search(
                f"{source.item.get('title') or ''}\n{source.item.get('content') or ''}",
                max(20, len(pool)),
            )
        except Exception as error:
            self.index_diagnostics.append(type(error).__name__)
            return {}
        current = {task["task_id"]: task for task in pool}
        scores: dict[str, float] = {}
        for hit in hits:
            task = current.get(hit.get("task_id"))
            score = float(hit.get("score") or 0.0)
            if (
                task is not None
                and hit.get("version") == task.get("version")
                and hit.get("last_event_id") == task.get("last_event_id")
                and 0.0 <= score <= 1.0
            ):
                scores[task["task_id"]] = score
            else:
                # Unknown, stale or out-of-range: recorded and dropped, never
                # trusted to supply task fields.
                self.index_diagnostics.append("stale_or_foreign_hit")
        return scores

    @staticmethod
    def _score(
        source: SourceContext,
        task: dict[str, Any],
        semantic: dict[str, float] | None = None,
    ) -> tuple[float, list[str], dict[str, Any]]:
        """Semantic cosine leads; assignee overlap and status only break ties."""
        item = source.item
        similarity = float((semantic or {}).get(task["task_id"], 0.0))
        if not semantic:
            similarity = goal_similarity(item, task)
        assignee_score = _assignee_overlap(
            item.get("assignee") or [], task.get("assignees") or []
        )
        score = 0.85 * similarity + 0.10 * assignee_score
        reasons = [
            f"semantic:{similarity:.3f}",
            f"assignee:{assignee_score:.3f}",
        ]
        if source.project_entity_id and (
            task.get("project_entity_id") == source.project_entity_id
        ):
            score += 0.05
            reasons.append("same_project_entity")
        if normalize_text(item.get("department")) and normalize_text(
            item.get("department")
        ) == normalize_text(task.get("department")):
            score += 0.03
            reasons.append("same_department")
        if task["status"] in ACTIVE:
            score += 0.02
            reasons.append("active_priority")
        return min(score, 1.0), reasons, task

    @staticmethod
    def _candidate_view(
        score: float, reasons: list[str], task: dict[str, Any]
    ) -> dict[str, Any]:
        fields = (
            "task_id",
            "item_type",
            "project_entity_id",
            "project",
            "source_project",
            "identity_uncertain",
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
