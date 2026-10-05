"""Neo4j-backed long-term memory for M3 project identity.

The instance is **shared** with an unrelated production project (labels
``Entity`` / ``Chunk`` / ``MilvusKB``), and Neo4j Community allows only one
user database. Every node type here therefore carries an ``M3Mem`` label
prefix, every relationship an ``M3MEM_`` prefix, and no query in this module
ever matches or deletes without that prefix. A typo in a label cannot silently
reach the neighbour's data.

What the memory stores:

``M3MemProject``
    A canonical project identity, e.g. ``红沙泉项目``. One node per family.

``M3MemSurface``
    Every project name actually observed in a document, with its 1024-dim
    bge-m3 vector, how often it was seen, and whether it was seen in the
    authoritative ``project`` field or only buried inside ``content``.
    Status is ``CANONICAL`` / ``ALIAS`` / ``TYPO`` / ``INDEPENDENT``.

``M3MemRule``
    A reusable decision, so the same judgement is never paid for twice:
    ``FAMILY_COLLAPSE`` (all ``红沙泉*`` → ``红沙泉项目``), ``TYPO_MAP``
    (``乌冬项目`` → ``乌东项目``) or ``KEEP_SEPARATE``.

``M3MemSemantic``
    Cached LLM reading of an evidence sentence (is this completion definite?
    is it negated? is it a transfer?), keyed by evidence text so repeated
    wording across meetings reuses one decision.

Vectors are queried through Neo4j's native ``db.index.vector.queryNodes``,
which Community 5.26 provides, so recall and graph traversal share one store.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .embedding_client import VECTOR_DIMENSIONS, EmbeddingClient

NEO4J_URI = os.getenv("M3_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.getenv("M3_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("M3_NEO4J_PASSWORD", "YOUR_PASSWORD")
NEO4J_DATABASE = os.getenv("M3_NEO4J_DATABASE", "neo4j")

SURFACE_INDEX = "m3mem_surface_vector"
VECTOR_RECALL_K = int(os.getenv("M3_VECTOR_RECALL_K", "24"))


def neo4j_score_to_cosine(score: float) -> float:
    """Neo4j reports cosine as ``(1 + cos) / 2``; every threshold in this project
    is written against true cosine, so convert once at the boundary.

    Verified against a probe index: unit vectors at cosine 0.9938 came back as
    0.9969, and ``陶忽图项目`` vs ``红沙泉项目`` came back as 0.7523 for a measured
    cosine of 0.5053.
    """
    return 2.0 * float(score) - 1.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_key(*parts: Any) -> str:
    joined = "\u241f".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def normalize_surface(value: Any) -> str:
    """Fold whitespace/case only. No regex business rules live here."""
    return " ".join(str(value or "").split()).casefold()


class MemoryUnavailable(RuntimeError):
    """The memory graph could not be reached or did not accept a write."""


class SemanticMemory:
    """Read/write access to the M3 identity memory."""

    def __init__(
        self,
        embedder: EmbeddingClient,
        *,
        uri: str = NEO4J_URI,
        user: str = NEO4J_USER,
        password: str = NEO4J_PASSWORD,
        database: str = NEO4J_DATABASE,
        driver: Any | None = None,
    ) -> None:
        self.embedder = embedder
        self.database = database
        self._lock = threading.RLock()
        if driver is None:
            from neo4j import GraphDatabase

            driver = GraphDatabase.driver(
                uri,
                auth=(user, password),
                # An empty graph legitimately has no M3Mem labels yet; those
                # "label does not exist" notices would otherwise flood the run log.
                notifications_min_severity="OFF",
            )
        self._driver = driver
        self.reads = 0
        self.writes = 0
        self.vector_queries = 0

    # ------------------------------------------------------------------ setup

    def verify(self) -> dict[str, Any]:
        try:
            self._driver.verify_connectivity()
        except Exception as error:  # pragma: no cover - transport failure
            raise MemoryUnavailable(
                f"cannot reach memory graph at {NEO4J_URI}: "
                f"{type(error).__name__}: {error}"
            ) from error
        with self._session() as session:
            counts = session.run(
                "OPTIONAL MATCH (p:M3MemProject) "
                "OPTIONAL MATCH (s:M3MemSurface) "
                "OPTIONAL MATCH (r:M3MemRule) "
                "RETURN count(DISTINCT p) AS projects, count(DISTINCT s) AS surfaces, "
                "count(DISTINCT r) AS rules"
            ).single()
        return {key: counts[key] for key in ("projects", "surfaces", "rules")}

    def ensure_schema(self) -> None:
        """Idempotent constraints plus the vector index."""
        statements = [
            "CREATE CONSTRAINT m3mem_project_key IF NOT EXISTS "
            "FOR (n:M3MemProject) REQUIRE n.project_key IS UNIQUE",
            "CREATE CONSTRAINT m3mem_surface_key IF NOT EXISTS "
            "FOR (n:M3MemSurface) REQUIRE n.surface_key IS UNIQUE",
            "CREATE CONSTRAINT m3mem_rule_key IF NOT EXISTS "
            "FOR (n:M3MemRule) REQUIRE n.rule_key IS UNIQUE",
            "CREATE CONSTRAINT m3mem_semantic_key IF NOT EXISTS "
            "FOR (n:M3MemSemantic) REQUIRE n.semantic_key IS UNIQUE",
            "CREATE INDEX m3mem_surface_family IF NOT EXISTS "
            "FOR (n:M3MemSurface) ON (n.family_key)",
            "CREATE INDEX m3mem_project_family IF NOT EXISTS "
            "FOR (n:M3MemProject) ON (n.family_key)",
        ]
        with self._session() as session:
            for statement in statements:
                session.run(statement).consume()
            session.run(
                "CREATE VECTOR INDEX $name IF NOT EXISTS "
                "FOR (n:M3MemSurface) ON (n.embedding) "
                "OPTIONS {indexConfig: {"
                "`vector.dimensions`: $dimensions, "
                "`vector.similarity_function`: 'cosine'}}",
                name=SURFACE_INDEX,
                dimensions=VECTOR_DIMENSIONS,
            ).consume()
            session.run("CALL db.awaitIndexes(30)").consume()

    def reset(self) -> dict[str, int]:
        """Drop **only** M3Mem data. Never touches other labels."""
        removed: dict[str, int] = {}
        with self._session() as session:
            for label in (
                "M3MemSurface",
                "M3MemProject",
                "M3MemRule",
                "M3MemSemantic",
            ):
                summary = session.run(
                    f"MATCH (n:{label}) DETACH DELETE n RETURN count(n) AS c"
                ).single()
                removed[label] = summary["c"]
        return removed

    # ------------------------------------------------------------------ write

    def observe_surface(
        self,
        text: str,
        *,
        document: str,
        authority: str = "FIELD",
        embedding: Sequence[float] | None = None,
        task_id: str | None = None,
    ) -> None:
        """Record that a surface form was seen; bumps frequency, never rewrites status."""
        if not str(text or "").strip():
            return
        if authority not in {"FIELD", "CONTENT"}:
            raise ValueError(f"authority must be FIELD or CONTENT, got {authority!r}")
        vector = list(embedding) if embedding is not None else self.embedder.embed_one(text)
        key = stable_key("surface", normalize_surface(text))
        with self._session() as session, self._lock:
            self.writes += 1
            session.run(
                """
                MERGE (s:M3MemSurface {surface_key: $key})
                ON CREATE SET s.text = $text, s.normalized = $normalized,
                              s.embedding = $embedding, s.frequency = 0,
                              s.field_count = 0, s.content_count = 0,
                              s.first_document = $document, s.status = 'UNRESOLVED',
                              s.created_at = $now
                SET s.last_document = $document, s.updated_at = $now,
                    s.frequency = s.frequency + 1,
                    s.field_count = s.field_count + CASE WHEN $authority = 'FIELD' THEN 1 ELSE 0 END,
                    s.content_count = s.content_count + CASE WHEN $authority = 'CONTENT' THEN 1 ELSE 0 END
                WITH s
                WHERE $task_id IS NOT NULL AND NOT (s)-[:M3MEM_SEEN_IN]->(:M3MemTask {task_id: $task_id})
                MERGE (t:M3MemTask {task_id: $task_id})
                MERGE (s)-[:M3MEM_SEEN_IN {document: $document}]->(t)
                """,
                key=key,
                text=str(text).strip(),
                normalized=normalize_surface(text),
                embedding=vector,
                document=document,
                authority=authority,
                task_id=task_id,
                now=utc_now(),
            ).consume()

    def upsert_project(
        self,
        canonical_name: str,
        *,
        family_key: str,
        family_text: str,
        document: str,
    ) -> str:
        project_key = stable_key("project", normalize_surface(canonical_name))
        with self._session() as session, self._lock:
            self.writes += 1
            session.run(
                """
                MERGE (p:M3MemProject {project_key: $project_key})
                ON CREATE SET p.canonical_name = $canonical_name, p.created_at = $now,
                              p.first_document = $document, p.task_count = 0
                SET p.family_key = $family_key, p.family_text = $family_text,
                    p.last_document = $document, p.updated_at = $now
                """,
                project_key=project_key,
                canonical_name=canonical_name,
                family_key=family_key,
                family_text=family_text,
                document=document,
                now=utc_now(),
            ).consume()
        return project_key

    def bind_surface(
        self,
        text: str,
        *,
        canonical_name: str,
        family_key: str,
        family_text: str,
        status: str,
        decided_by: str,
        reason: str,
        document: str,
        score: float | None = None,
        corrected_from: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> dict[str, str]:
        """Attach a surface form to a canonical project and remember why."""
        if status not in {"CANONICAL", "ALIAS", "TYPO", "INDEPENDENT"}:
            raise ValueError(f"unknown surface status {status!r}")
        project_key = self.upsert_project(
            canonical_name,
            family_key=family_key,
            family_text=family_text,
            document=document,
        )
        surface_key = stable_key("surface", normalize_surface(text))
        vector = (
            list(embedding)
            if embedding is not None
            else self.embedder.embed_one(text)
        )
        with self._session() as session, self._lock:
            self.writes += 1
            session.run(
                """
                MERGE (s:M3MemSurface {surface_key: $surface_key})
                ON CREATE SET s.text = $text, s.normalized = $normalized,
                              s.embedding = $embedding, s.frequency = 0,
                              s.field_count = 0, s.content_count = 0,
                              s.first_document = $document, s.created_at = $now
                SET s.status = $status, s.decided_by = $decided_by,
                    s.reason = $reason, s.score = $score,
                    s.canonical_name = $canonical_name, s.family_key = $family_key,
                    s.decided_at = $now, s.updated_at = $now
                WITH s
                MATCH (p:M3MemProject {project_key: $project_key})
                MERGE (s)-[r:M3MEM_RESOLVES_TO]->(p)
                SET r.decided_by = $decided_by, r.reason = $reason,
                    r.score = $score, r.decided_at = $now
                """,
                surface_key=surface_key,
                text=str(text).strip(),
                normalized=normalize_surface(text),
                embedding=vector,
                status=status,
                decided_by=decided_by,
                reason=reason[:1000],
                score=score,
                canonical_name=canonical_name,
                family_key=family_key,
                project_key=project_key,
                document=document,
                now=utc_now(),
            ).consume()
            if corrected_from:
                source_key = stable_key("surface", normalize_surface(corrected_from))
                session.run(
                    """
                    MATCH (wrong:M3MemSurface {surface_key: $surface_key})
                    MERGE (right:M3MemSurface {surface_key: $source_key})
                    ON CREATE SET right.text = $corrected_from,
                                  right.normalized = $corrected_normalized,
                                  right.status = 'CANONICAL',
                                  right.created_at = $now
                    MERGE (wrong)-[c:M3MEM_CORRECTS_TO]->(right)
                    SET c.reason = $reason, c.decided_by = $decided_by, c.decided_at = $now
                    """,
                    surface_key=surface_key,
                    source_key=source_key,
                    corrected_from=corrected_from,
                    corrected_normalized=normalize_surface(corrected_from),
                    reason=reason[:1000],
                    decided_by=decided_by,
                    now=utc_now(),
                ).consume()
        return {"project_key": project_key, "surface_key": surface_key}

    def remember_rule(
        self,
        *,
        kind: str,
        premise: str,
        conclusion: str,
        decided_by: str,
        reason: str,
        document: str,
        evidence: str = "",
        family_key: str | None = None,
    ) -> str:
        if kind not in {"FAMILY_COLLAPSE", "TYPO_MAP", "KEEP_SEPARATE"}:
            raise ValueError(f"unknown rule kind {kind!r}")
        rule_key = stable_key("rule", kind, normalize_surface(premise), normalize_surface(conclusion))
        with self._session() as session, self._lock:
            self.writes += 1
            session.run(
                """
                MERGE (r:M3MemRule {rule_key: $rule_key})
                ON CREATE SET r.kind = $kind, r.premise = $premise,
                              r.conclusion = $conclusion, r.hits = 0,
                              r.created_at = $now, r.first_document = $document
                SET r.decided_by = $decided_by, r.reason = $reason,
                    r.evidence = $evidence, r.last_document = $document,
                    r.family_key = $family_key, r.updated_at = $now
                """,
                rule_key=rule_key,
                kind=kind,
                premise=premise,
                conclusion=conclusion,
                decided_by=decided_by,
                reason=reason[:1000],
                evidence=evidence[:2000],
                family_key=family_key,
                document=document,
                now=utc_now(),
            ).consume()
        return rule_key

    def count_rule_hit(self, rule_key: str) -> None:
        with self._session() as session, self._lock:
            self.writes += 1
            session.run(
                "MATCH (r:M3MemRule {rule_key: $rule_key}) "
                "SET r.hits = coalesce(r.hits, 0) + 1, r.last_hit_at = $now",
                rule_key=rule_key,
                now=utc_now(),
            ).consume()

    def link_task(self, project_key: str, task_id: str, *, document: str) -> None:
        with self._session() as session, self._lock:
            self.writes += 1
            session.run(
                """
                MATCH (p:M3MemProject {project_key: $project_key})
                MERGE (t:M3MemTask {task_id: $task_id})
                ON CREATE SET t.first_document = $document
                MERGE (p)-[l:M3MEM_HAS_TASK]->(t)
                SET l.last_document = $document
                WITH p
                SET p.task_count = size((p)-[:M3MEM_HAS_TASK]->()), p.updated_at = $now
                """,
                project_key=project_key,
                task_id=task_id,
                document=document,
                now=utc_now(),
            ).consume()

    # ------------------------------------------------------------------- read

    def lookup_surface(self, text: str) -> dict[str, Any] | None:
        key = stable_key("surface", normalize_surface(text))
        with self._session() as session:
            self.reads += 1
            row = session.run(
                """
                MATCH (s:M3MemSurface {surface_key: $key})
                OPTIONAL MATCH (s)-[:M3MEM_RESOLVES_TO]->(p:M3MemProject)
                OPTIONAL MATCH (s)-[:M3MEM_CORRECTS_TO]->(right:M3MemSurface)
                RETURN s.text AS text, s.status AS status, s.decided_by AS decided_by,
                       s.reason AS reason, s.score AS score, s.frequency AS frequency,
                       s.field_count AS field_count, s.content_count AS content_count,
                       s.family_key AS family_key, s.canonical_name AS canonical_name,
                       s.first_document AS first_document, s.last_document AS last_document,
                       p.project_key AS project_key, p.canonical_name AS project_name,
                       right.text AS corrected_to
                """,
                key=key,
            ).single()
        return dict(row) if row else None

    def vector_recall(
        self, text: str, *, k: int = VECTOR_RECALL_K
    ) -> list[dict[str, Any]]:
        """Nearest surface forms, highest cosine first."""
        vector = self.embedder.embed_one(text)
        with self._session() as session:
            self.reads += 1
            self.vector_queries += 1
            rows = session.run(
                """
                CALL db.index.vector.queryNodes($index, $k, $vector)
                YIELD node, score
                OPTIONAL MATCH (node)-[:M3MEM_RESOLVES_TO]->(p:M3MemProject)
                RETURN node.text AS text, node.status AS status,
                       (2.0 * score - 1.0) AS score,
                       node.frequency AS frequency, node.field_count AS field_count,
                       node.content_count AS content_count,
                       node.family_key AS family_key,
                       node.canonical_name AS canonical_name,
                       node.reason AS reason, node.decided_by AS decided_by,
                       p.project_key AS project_key, p.canonical_name AS project_name,
                       p.task_count AS task_count
                ORDER BY score DESC
                """,
                index=SURFACE_INDEX,
                k=int(k),
                vector=vector,
            ).data()
        return rows

    def family_rule(self, family_key: str) -> dict[str, Any] | None:
        with self._session() as session:
            self.reads += 1
            row = session.run(
                "MATCH (r:M3MemRule) WHERE r.kind = 'FAMILY_COLLAPSE' AND r.family_key = $family_key "
                "RETURN r.rule_key AS rule_key, r.kind AS kind, r.premise AS premise, "
                "r.conclusion AS conclusion, r.decided_by AS decided_by, r.reason AS reason, r.hits AS hits "
                "ORDER BY r.updated_at DESC LIMIT 1",
                family_key=family_key,
            ).single()
        return dict(row) if row else None

    def find_rule(self, kind: str, premise: str, conclusion: str) -> dict[str, Any] | None:
        with self._session() as session:
            self.reads += 1
            row = session.run(
                "MATCH (r:M3MemRule {rule_key: $key}) "
                "RETURN r.kind AS kind, r.premise AS premise, r.conclusion AS conclusion, "
                "r.decided_by AS decided_by, r.reason AS reason, r.hits AS hits",
                key=stable_key(
                    "rule", kind, normalize_surface(premise), normalize_surface(conclusion)
                ),
            ).single()
        return dict(row) if row else None

    def family_members(self, family_key: str) -> list[dict[str, Any]]:
        with self._session() as session:
            self.reads += 1
            return session.run(
                """
                MATCH (s:M3MemSurface) WHERE s.family_key = $family_key
                OPTIONAL MATCH (s)-[:M3MEM_RESOLVES_TO]->(p:M3MemProject)
                RETURN s.text AS text, s.status AS status, s.frequency AS frequency,
                       s.field_count AS field_count, s.content_count AS content_count,
                       s.reason AS reason, s.decided_by AS decided_by,
                       s.first_document AS first_document, s.last_document AS last_document,
                       p.canonical_name AS canonical_name
                ORDER BY s.field_count DESC, s.frequency DESC, s.text
                """,
                family_key=family_key,
            ).data()

    def known_families(self) -> list[dict[str, Any]]:
        with self._session() as session:
            self.reads += 1
            return session.run(
                """
                MATCH (p:M3MemProject)
                OPTIONAL MATCH (s:M3MemSurface)-[:M3MEM_RESOLVES_TO]->(p)
                RETURN p.family_key AS family_key, p.family_text AS family_text,
                       p.canonical_name AS canonical_name, p.project_key AS project_key,
                       p.task_count AS task_count, p.first_document AS first_document,
                       count(s) AS surface_count,
                       sum(coalesce(s.frequency, 0)) AS observations
                ORDER BY observations DESC, p.canonical_name
                """
            ).data()

    # ------------------------------------------------------- semantic cache

    def semantic_flags(
        self, kind: str, evidence: str, subject: str = ""
    ) -> dict[str, Any] | None:
        key = stable_key("semantic", kind, normalize_surface(evidence), normalize_surface(subject))
        with self._session() as session:
            self.reads += 1
            row = session.run(
                "MATCH (n:M3MemSemantic {semantic_key: $key}) "
                "RETURN n.flags_json AS flags_json, n.hits AS hits, n.decided_at AS decided_at",
                key=key,
            ).single()
        if not row or not row["flags_json"]:
            return None
        return json.loads(row["flags_json"])

    def remember_semantic(
        self,
        kind: str,
        evidence: str,
        flags: dict[str, Any],
        *,
        subject: str = "",
        reason: str = "",
    ) -> None:
        key = stable_key("semantic", kind, normalize_surface(evidence), normalize_surface(subject))
        with self._session() as session, self._lock:
            self.writes += 1
            session.run(
                """
                MERGE (n:M3MemSemantic {semantic_key: $key})
                ON CREATE SET n.kind = $kind, n.evidence = $evidence, n.subject = $subject,
                              n.hits = 0, n.created_at = $now
                SET n.flags_json = $flags_json, n.reason = $reason, n.updated_at = $now
                """,
                key=key,
                kind=kind,
                evidence=str(evidence)[:2000],
                subject=str(subject)[:500],
                flags_json=json.dumps(flags, ensure_ascii=False, sort_keys=True),
                reason=reason[:1000],
                now=utc_now(),
            ).consume()

    def bump_semantic_hit(self, kind: str, evidence: str, subject: str = "") -> None:
        key = stable_key("semantic", kind, normalize_surface(evidence), normalize_surface(subject))
        with self._session() as session, self._lock:
            session.run(
                "MATCH (n:M3MemSemantic {semantic_key: $key}) "
                "SET n.hits = coalesce(n.hits, 0) + 1",
                key=key,
            ).consume()

    # ------------------------------------------------------------------ misc

    def export_snapshot(self) -> dict[str, Any]:
        """Whole M3Mem subgraph as plain data, for reports and diffing."""
        with self._session() as session:
            self.reads += 1
            projects = session.run(
                "MATCH (p:M3MemProject) RETURN p {.*} AS node ORDER BY p.canonical_name"
            ).data()
            surfaces = session.run(
                """
                MATCH (s:M3MemSurface)
                OPTIONAL MATCH (s)-[:M3MEM_RESOLVES_TO]->(p:M3MemProject)
                RETURN s {.*, embedding: null, canonical: p.canonical_name} AS node
                ORDER BY s.text
                """
            ).data()
            rules = session.run(
                "MATCH (r:M3MemRule) RETURN r {.*} AS node ORDER BY r.kind, r.premise"
            ).data()
            semantics = session.run(
                "MATCH (n:M3MemSemantic) RETURN n {.*} AS node ORDER BY n.kind"
            ).data()
        return {
            "exported_at": utc_now(),
            "projects": [row["node"] for row in projects],
            "surfaces": [row["node"] for row in surfaces],
            "rules": [row["node"] for row in rules],
            "semantics": [row["node"] for row in semantics],
        }

    def stats(self) -> dict[str, Any]:
        with self._session() as session:
            row = session.run(
                """
                OPTIONAL MATCH (s:M3MemSurface)
                OPTIONAL MATCH (r:M3MemRule)
                OPTIONAL MATCH (n:M3MemSemantic)
                WITH count(DISTINCT s) AS surfaces, count(DISTINCT r) AS rules,
                     count(DISTINCT n) AS semantics,
                     [x IN collect(DISTINCT s.status) WHERE x IS NOT NULL] AS statuses
                OPTIONAL MATCH (p:M3MemProject)
                RETURN surfaces, rules, semantics, count(DISTINCT p) AS projects
                """
            ).single()
            by_status = session.run(
                "MATCH (s:M3MemSurface) RETURN s.status AS status, count(*) AS c "
                "ORDER BY c DESC"
            ).data()
        return {
            "projects": row["projects"],
            "surfaces": row["surfaces"],
            "rules": row["rules"],
            "semantic_cache": row["semantics"],
            "surface_status": {item["status"]: item["c"] for item in by_status},
            "graph_reads": self.reads,
            "graph_writes": self.writes,
            "vector_queries": self.vector_queries,
            "embedding": self.embedder.stats(),
        }

    def _session(self):
        return self._driver.session(database=self.database)

    def close(self) -> None:
        with self._lock:
            self._driver.close()


def connect_memory(embedder: EmbeddingClient | None = None, **kwargs: Any) -> SemanticMemory:
    """Open the memory graph and make sure the schema exists."""
    memory = SemanticMemory(embedder or EmbeddingClient(), **kwargs)
    memory.verify()
    memory.ensure_schema()
    return memory


def iter_chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
