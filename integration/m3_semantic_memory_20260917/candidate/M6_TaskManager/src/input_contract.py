"""Validate native M1 items and retain one-to-one source provenance."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from .identifiers import source_item_id
from .models import InputContractError, SourceContext


@lru_cache(maxsize=1)
def item_validator() -> Draft7Validator:
    for ancestor in Path(__file__).resolve().parents[2:]:
        schema_path = ancestor / "M1_Extraction/schemas/m1_items.schema.json"
        if schema_path.is_file():
            return Draft7Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    raise InputContractError("M1 item schema is unavailable")


def validate_items(items: Any) -> None:
    errors = list(item_validator().iter_errors({"items": items}))
    if errors:
        raise InputContractError("Invalid M1 items: " + errors[0].message)
    for index, item in enumerate(items):
        evidence = item["evidence"]
        if not evidence["text"].strip() or evidence["start_char"] > evidence["end_char"]:
            raise InputContractError(f"Item {index} has invalid source evidence")
        if (evidence["page_start"] is not None and evidence["page_end"] is not None
                and evidence["page_start"] > evidence["page_end"]):
            raise InputContractError(f"Item {index} has an invalid page interval")


def context_from_item(
    item: dict[str, Any],
    *,
    document_id: str,
    item_index: int,
    mode: str = "generic",
    origin_document_id: str | None = None,
    source_id: str | None = None,
    project_entity_id: str | None = None,
) -> SourceContext:
    validate_items([item])
    if not isinstance(document_id, str) or not document_id.strip():
        raise InputContractError("A source document ID is required")
    if type(item_index) is not int or item_index < 0:
        raise InputContractError("The source item index must be a nonnegative integer")
    if mode not in ("generic", "block"):
        raise InputContractError("Unsupported M1 extraction mode")
    if origin_document_id is not None and (
        not isinstance(origin_document_id, str) or not origin_document_id.strip()
    ):
        raise InputContractError("Invalid origin document ID")
    trace = {
        "item_index": item_index,
        "source_indexes": [item_index],
        "source_evidence": [item["evidence"]],
    }
    project = item.get("project")
    entity = project_entity_id
    if entity is None and project:
        entity = "RAW-" + hashlib.sha256(project.encode()).hexdigest()[:20]
    return SourceContext(
        source_document_id=document_id,
        source_item_id=source_id or source_item_id(document_id, item_index, item, trace),
        source_mode=mode,
        item_index=item_index,
        item=item,
        source_trace=trace,
        project_entity_id=entity,
        input_validation={"status": "PASS", "issues": []},
        input_stage="M1",
        origin_document_id=origin_document_id or document_id,
    )
