"""Stable source identifiers used for idempotency and audit."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"[^A-Za-z0-9_.:-]+")


def _digest(payload: Any, length: int = 24) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def source_document_id(
    m2_payload: dict[str, Any],
    input_path: Path | str | None = None,
    explicit_id: str | None = None,
) -> str:
    if explicit_id:
        cleaned = SAFE_ID.sub("_", explicit_id.strip())
        if cleaned:
            return cleaned[:120]
    stable_payload = {
        "source_mode": m2_payload.get("source_mode"),
        "items": m2_payload.get("items", []),
        "merge_trace": m2_payload.get("merge_trace", []),
    }
    prefix = "document"
    if input_path:
        prefix = SAFE_ID.sub("_", Path(input_path).stem).strip("_") or prefix
    return f"{prefix[:60]}:{_digest(stable_payload)}"


def source_item_id(
    document_id: str,
    item_index: int,
    item: dict[str, Any],
    merge_trace: dict[str, Any],
) -> str:
    evidence = item.get("evidence") or {}
    stable_payload = {
        "document_id": document_id,
        "source_indexes": merge_trace.get("source_indexes", [item_index]),
        "start_char": evidence.get("start_char"),
        "end_char": evidence.get("end_char"),
        "content": item.get("content"),
        "item_type": item.get("item_type"),
    }
    return f"ITEM:{_digest(stable_payload)}"
