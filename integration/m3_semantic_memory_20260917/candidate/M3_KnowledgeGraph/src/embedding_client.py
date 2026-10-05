"""bge-m3 dense embedding + bge-reranker client for M3 semantic recall.

Both services run on the local Iluvatar accelerator and speak the OpenAI
``/v1/embeddings`` shape. Recall scores never decide a lifecycle action on
their own; they only choose which candidates are worth asking the model about.

A transport failure raises ``EmbeddingServiceError`` instead of degrading to a
lexical fallback, so a silent loss of semantic recall cannot be mistaken for a
clean run.
"""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Any, Iterable, Sequence

import httpx

EMBEDDING_URL = os.getenv("M3_EMBEDDING_URL", "http://127.0.0.1:8000/v1/embeddings")
RERANK_URL = os.getenv("M3_RERANK_URL", "http://127.0.0.1:8003/v1/rerank")
EMBEDDING_MODEL = os.getenv("M3_EMBEDDING_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.getenv("M3_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
VECTOR_DIMENSIONS = int(os.getenv("M3_VECTOR_DIMENSIONS", "1024"))
MAX_TEXTS_PER_REQUEST = int(os.getenv("M3_EMBEDDING_BATCH", "32"))
REQUEST_TIMEOUT = float(os.getenv("M3_EMBEDDING_TIMEOUT", "120"))


class EmbeddingServiceError(RuntimeError):
    """The embedding or rerank service is unreachable or returned bad data."""


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / ((norm_left ** 0.5) * (norm_right ** 0.5))


def text_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


class EmbeddingClient:
    """Thread-safe, cached client. The pipeline runs up to four worker threads."""

    def __init__(
        self,
        *,
        embedding_url: str = EMBEDDING_URL,
        rerank_url: str = RERANK_URL,
        embedding_model: str = EMBEDDING_MODEL,
        rerank_model: str = RERANK_MODEL,
        batch_size: int = MAX_TEXTS_PER_REQUEST,
        timeout: float = REQUEST_TIMEOUT,
        cache_limit: int = 20000,
    ) -> None:
        self.embedding_url = embedding_url
        self.rerank_url = rerank_url
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model
        self.batch_size = max(1, min(batch_size, 64))
        self.timeout = timeout
        self.cache_limit = cache_limit
        self._cache: dict[str, list[float]] = {}
        self._lock = threading.RLock()
        self._client = httpx.Client(timeout=timeout)
        self.calls = 0
        self.texts_embedded = 0
        self.cache_hits = 0
        self.rerank_calls = 0

    def close(self) -> None:
        with self._lock:
            self._client.close()

    def __enter__(self) -> "EmbeddingClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def embed_one(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text, preserving order."""
        if not texts:
            return []
        vectors: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        with self._lock:
            for index, text in enumerate(texts):
                key = text_key(text)
                cached = self._cache.get(key)
                if cached is not None:
                    vectors[index] = cached
                    self.cache_hits += 1
                else:
                    missing.append(index)
        if missing:
            fresh = self._request_vectors([texts[index] for index in missing])
            with self._lock:
                for position, index in enumerate(missing):
                    vectors[index] = fresh[position]
                    if len(self._cache) >= self.cache_limit:
                        self._cache.pop(next(iter(self._cache)), None)
                    self._cache[text_key(texts[index])] = fresh[position]
        return [vector for vector in vectors if vector is not None]

    def _request_vectors(self, texts: Sequence[str]) -> list[list[float]]:
        collected: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = list(texts[start : start + self.batch_size])
            payload = {
                "model": self.embedding_model,
                "input": chunk,
                "max_length": 256,
            }
            try:
                with self._lock:
                    self.calls += 1
                    response = self._client.post(self.embedding_url, json=payload)
                    response.raise_for_status()
                    body = response.json()
            except httpx.HTTPError as error:
                raise EmbeddingServiceError(
                    f"embedding service failed: {type(error).__name__}: {error}"
                ) from error
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list) or len(data) != len(chunk):
                raise EmbeddingServiceError(
                    f"embedding service returned {len(data) if isinstance(data, list) else data!r} "
                    f"vectors for {len(chunk)} texts"
                )
            ordered = sorted(data, key=lambda row: row.get("index", 0))
            for row in ordered:
                vector = row.get("embedding")
                if not isinstance(vector, list) or not vector:
                    raise EmbeddingServiceError("embedding service returned an empty vector")
                if len(vector) != VECTOR_DIMENSIONS:
                    raise EmbeddingServiceError(
                        f"embedding dimension {len(vector)} != expected {VECTOR_DIMENSIONS}"
                    )
                collected.append([float(value) for value in vector])
            self.texts_embedded += len(chunk)
        return collected

    def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        """Cross-encoder scores, highest first, as ``(index, score)`` pairs."""
        if not query.strip() or not documents:
            return []
        payload = {
            "model": self.rerank_model,
            "query": query,
            "documents": list(documents),
        }
        try:
            with self._lock:
                self.rerank_calls += 1
                response = self._client.post(self.rerank_url, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as error:
            raise EmbeddingServiceError(
                f"rerank service failed: {type(error).__name__}: {error}"
            ) from error
        rows = body.get("results", body.get("data")) if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise EmbeddingServiceError("rerank service returned no results")
        scored: list[tuple[int, float]] = []
        for row in rows:
            index = row.get("index")
            score = row.get("relevance_score", row.get("score"))
            if isinstance(index, int) and isinstance(score, (int, float)):
                scored.append((index, float(score)))
        scored.sort(key=lambda pair: -pair[1])
        return scored

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "embedding_calls": self.calls,
                "texts_embedded": self.texts_embedded,
                "cache_hits": self.cache_hits,
                "cache_size": len(self._cache),
                "rerank_calls": self.rerank_calls,
            }


def dedupe(texts: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for text in texts:
        if text and text not in seen:
            seen[text] = None
    return list(seen)
