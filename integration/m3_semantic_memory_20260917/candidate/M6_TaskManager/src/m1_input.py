"""Load the native M1 nine-field envelope without an intermediate contract."""

from __future__ import annotations

import json
from pathlib import Path

from .input_contract import context_from_item, validate_items
from .models import InputContractError, SourceContext


def load_m1_payload(
    path: Path | str, *, document_id: str | None = None
) -> tuple[str, list[SourceContext]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InputContractError("Invalid M1 JSON") from error
    if not isinstance(payload, dict) or set(payload) - {"items", "mode", "source_document_id"}:
        raise InputContractError("Unsupported M1 envelope fields")
    validate_items(payload.get("items"))
    origin = payload.get("source_document_id")
    if origin is not None and (not isinstance(origin, str) or not origin.strip()):
        raise InputContractError("Invalid origin document ID")
    resolved_id = document_id if document_id is not None else origin
    if not isinstance(resolved_id, str) or not resolved_id.strip():
        raise InputContractError("A source document ID is required")
    mode = payload.get("mode", "generic")
    if mode not in ("generic", "block"):
        raise InputContractError("Unsupported M1 extraction mode")
    contexts = [
        context_from_item(item, document_id=resolved_id, item_index=index,
                          mode=mode, origin_document_id=origin or resolved_id)
        for index, item in enumerate(payload["items"])
    ]
    return resolved_id, contexts
