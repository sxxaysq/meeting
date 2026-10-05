"""M3 read-only project/name recall over committed M6 audits.

Supplies the candidate parents that M6 shows the model. It never decides:
ranking is bge-m3 cosine plus, when the memory graph is reachable, the family
and typo decisions already recorded there.

``meeting_date`` keeps its regex on purpose — parsing the document id into a
date is what makes the replay chronological, and losing it would let a later
meeting's tasks be recalled as history by an earlier one.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from typing import Any

from .embedding_client import EmbeddingClient, cosine

_DATE_PATTERN = re.compile(r"(20\d{2})[-.年](\d{1,2})[-.月](\d{1,2})")

# Recall floor only. Cross-project similarity measured on this dataset tops out
# at 0.720, so 0.40 admits every plausible relative and leaves the actual
# judgement to the model.
RECALL_FLOOR = 0.40


def meeting_date(document: str) -> date | None:
    match = _DATE_PATTERN.search(str(document or ""))
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


class ProjectMemory:
    """Prior-meeting project recall, semantically ranked."""

    def __init__(
        self,
        repository: Any,
        embedder: EmbeddingClient | None = None,
        resolver: Any | None = None,
    ) -> None:
        self.repository = repository
        self.embedder = embedder
        self.resolver = resolver
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._cache_lock = threading.RLock()
        self._vectors: dict[str, list[float]] = {}
        self.vector_queries = 0

    def prior_memories(self, document: str) -> list[dict[str, Any]]:
        """Committed project assignments from meetings strictly before this one."""
        cutoff = meeting_date(document)
        if not cutoff:
            return []
        with self._cache_lock:
            if document in self._cache:
                return self._cache[document]
        with self.repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.audit_id, a.source_document_id, a.source_item_id,
                       a.provenance_json, a.task_id, t.project, t.project_entity_id,
                       t.source_project, t.status, t.version
                FROM task_audit a
                JOIN tasks t ON t.task_id = a.task_id
                WHERE a.action IN ('CREATE','PROGRESS_UPDATE','MODIFY','COMPLETE',
                                   'REOPEN','CANCEL','TRANSFER')
                ORDER BY a.rowid
                """
            ).fetchall()
        memories: list[dict[str, Any]] = []
        for row in rows:
            prior = meeting_date(row["source_document_id"])
            if prior is None or prior >= cutoff:
                continue
            if not row["project"]:
                continue
            try:
                provenance = json.loads(row["provenance_json"])
            except (TypeError, ValueError):
                provenance = {}
            scope = provenance.get("admission") or {}
            original = scope.get("original_item") or provenance.get("item") or {}
            memories.append(
                {
                    "project_id": row["project_entity_id"],
                    "parent_project": row["project"],
                    "observed_name": original.get("project") or row["project"],
                    "source_project_id": scope.get("source_project_id"),
                    "confirmed_parent": scope.get("scope_validated") is True,
                    "scope": row["source_project"],
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "version": row["version"],
                    "source_document_id": row["source_document_id"],
                    "source_item_id": row["source_item_id"],
                    "audit_id": row["audit_id"],
                    "evidence": str((original.get("evidence") or {}).get("text") or "")[:240],
                }
            )
        with self._cache_lock:
            self._cache[document] = memories
        return memories

    def _vector(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        with self._cache_lock:
            cached = self._vectors.get(text)
        if cached is not None:
            return cached
        try:
            vector = self.embedder.embed_one(text)
        except Exception:
            return None
        with self._cache_lock:
            if len(self._vectors) > 20000:
                self._vectors.pop(next(iter(self._vectors)), None)
            self._vectors[text] = vector
        return vector

    def retrieve_names(
        self, name: str, document: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Nearest previously-seen project names, best first.

        Each row keeps the shape M6 already consumes. ``retrieval_score`` is now
        cosine similarity; ``memory_status`` and ``canonical_name`` are added
        when the memory graph has already resolved either side, so the model can
        see that a name is a known alias or a known typo rather than guessing.
        """
        text = str(name or "").strip()
        memories = self.prior_memories(document)
        if not text or not memories:
            return []
        query = self._vector(text)
        if query is not None:
            self.vector_queries += 1
        best: dict[tuple[Any, str], dict[str, Any]] = {}
        for row in memories:
            if query is None:
                score = 0.0
            else:
                observed = self._vector(str(row["observed_name"]))
                parent = self._vector(str(row["parent_project"]))
                score = max(
                    cosine(query, observed) if observed else 0.0,
                    cosine(query, parent) if parent else 0.0,
                )
            if score < RECALL_FLOOR:
                continue
            enriched = dict(row)
            enriched["retrieval_score"] = round(score, 4)
            memory_row = self._memory_row(str(row["observed_name"]))
            if memory_row:
                enriched["memory_status"] = memory_row.get("status")
                enriched["canonical_name"] = memory_row.get("canonical_name")
                enriched["occurrences"] = memory_row.get("frequency")
                enriched["field_count"] = memory_row.get("field_count")
            key = (row["project_id"], row["parent_project"])
            current = best.get(key)
            if current is None or score > current["retrieval_score"]:
                best[key] = enriched
        ranked = sorted(
            best.values(),
            key=lambda row: (-row["retrieval_score"], row["parent_project"]),
        )[: max(1, limit)]
        return [dict(row, index=index) for index, row in enumerate(ranked)]

    def _memory_row(self, text: str) -> dict[str, Any] | None:
        resolver = self.resolver
        if resolver is None:
            return None
        memory = getattr(resolver, "memory", None)
        if memory is None:
            return None
        try:
            return memory.lookup_surface(text)
        except Exception:
            return None

    def invalidate(self, document: str | None = None) -> None:
        """Drop cached audits after new tasks are committed."""
        with self._cache_lock:
            if document is None:
                self._cache.clear()
            else:
                self._cache.pop(document, None)

    def stats(self) -> dict[str, Any]:
        with self._cache_lock:
            return {
                "cached_documents": len(self._cache),
                "cached_vectors": len(self._vectors),
                "vector_queries": self.vector_queries,
            }
