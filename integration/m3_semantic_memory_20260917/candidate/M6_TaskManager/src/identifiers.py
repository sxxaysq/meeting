"""Stable source identifiers used for idempotency and audit."""

from __future__ import annotations

import hashlib
import json
from typing import Any




def _digest(payload: Any, length: int = 24) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]




def source_item_id(
    document_id: str,
    item_index: int,
    item: dict[str, Any],
    source_trace: dict[str, Any],
) -> str:
    evidence = item.get("evidence") or {}
    stable_payload = {
        "document_id": document_id,
        "source_indexes": source_trace.get("source_indexes", [item_index]),
        "start_char": evidence.get("start_char"),
        "end_char": evidence.get("end_char"),
        "content": item.get("content"),
        "item_type": item.get("item_type"),
    }
    return f"ITEM:{_digest(stable_payload)}"
