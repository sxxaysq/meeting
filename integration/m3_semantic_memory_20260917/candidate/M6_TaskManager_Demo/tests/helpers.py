"""测试命令和 M2 记录构造。"""

from __future__ import annotations

from app.m6.model_client import empty_patch


def source_record(
    text: str,
    category: str,
    segment_id: str = "seg_001",
    subsegment_id: str = "001",
    speaker_id: str | None = "机电队",
) -> dict:
    missing = ["start_ms", "end_ms", "asr_confidence", "nbest"]
    if speaker_id is None:
        missing.append("speaker_id")
    return {
        "segment_id": segment_id,
        "subsegment_id": subsegment_id,
        "speaker_id": speaker_id,
        "speaker_confidence": 0.9 if speaker_id else 0.0,
        "start_ms": None,
        "end_ms": None,
        "text_raw": text,
        "asr_confidence": None,
        "nbest": [],
        "missing_fields": missing,
        "task_category": category,
    }


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

