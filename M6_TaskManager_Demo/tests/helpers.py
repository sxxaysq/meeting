"""Offline model stub and source-grounded command fixtures."""

from __future__ import annotations

from app.m6.model_client import empty_patch


class OfflineReviewClient:
    """Make unexpected advisory-model requests fail without network access."""

    model = "offline-review-test"
    client = None


def command_batch(
    source: dict,
    meeting_id: str,
    action: str,
    patch: dict | None = None,
    target_task_id: str | None = None,
    expected_version: int | None = None,
    reason_code: str = "TEST",
) -> dict:
    return {
        "schema_version": "m6_db_command_v1",
        "source_key": (
            f"{meeting_id}:{source['segment_id']}:{source['subsegment_id']}"
        ),
        "commands": [
            {
                "command_index": 1,
                "db_action": action,
                "target_task_id": target_task_id,
                "expected_version": expected_version,
                "task_patch": patch or empty_patch(),
                "evidence": {
                    "segment_id": source["segment_id"],
                    "subsegment_id": source["subsegment_id"],
                    "text": source["text"],
                },
                "reason_code": reason_code,
                "ambiguities": [],
            }
        ],
    }

