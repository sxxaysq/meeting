"""Model connection for advisory human-review plans and command fields."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol


class ReviewClient(Protocol):
    client: Any
    model: str


def _parse_json_object(content: str) -> dict:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Review response must be a JSON object")
    return parsed


class OpenAICompatibleReviewClient:
    """Connect to the model used only to propose editable review forms."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        timeout_seconds: float = 120,
    ) -> None:
        from openai import OpenAI

        self.client = OpenAI(
            api_key=api_key or "EMPTY",
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=1,
        )
        self.model = model


def empty_patch(**overrides: object) -> dict:
    patch = {
        "title": None,
        "description": None,
        "work_items": None,
        "assignee_raw": None,
        "deadline_raw": None,
        "status": None,
    }
    patch.update(overrides)
    return patch
