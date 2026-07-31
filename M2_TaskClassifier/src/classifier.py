"""提示词版 M2 任务候选分类主流程。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from model_client import ModelClient
from prompt_builder import build_repair_prompt, build_user_prompt
from response_validator import (
    ResponseValidationError,
    enforce_recall_routes,
    parse_and_validate_response,
)


@dataclass
class ClassificationFailure(Exception):
    segment_id: str
    message: str
    attempts: list[dict]

    def __str__(self) -> str:
        return f"{self.segment_id}: {self.message}"


def build_context_records(records: list[dict], context_chars: int) -> list[dict]:
    """只在同一 meeting_title 内取前后各一条 M1 片段。"""
    windows: list[dict] = []
    for index, record in enumerate(records):
        before = ""
        after = ""
        if index > 0 and records[index - 1]["meeting_title"] == record["meeting_title"]:
            before = records[index - 1]["clean_text"][-context_chars:]
        if (
            index + 1 < len(records)
            and records[index + 1]["meeting_title"] == record["meeting_title"]
        ):
            after = records[index + 1]["clean_text"][:context_chars]
        windows.append({"record": record, "context_before": before, "context_after": after})
    return windows


def classify_one(
    record: dict,
    context_before: str,
    context_after: str,
    system_prompt: str,
    schema: dict,
    client: ModelClient,
    repair_attempts: int = 1,
) -> tuple[dict, dict]:
    """分类一个片段；无效响应最多修复一次。"""
    user_prompt = build_user_prompt(record, context_before, context_after)
    current_prompt = user_prompt
    attempts: list[dict] = []
    current_text = record["clean_text"]
    last_error = "未知校验错误"

    for attempt_index in range(repair_attempts + 1):
        response = client.generate(system_prompt, current_prompt)
        audit_attempt = {
            "attempt": attempt_index + 1,
            "model": response.get("model"),
            "usage": response.get("usage", {}),
            "response": response.get("content", ""),
        }
        try:
            result, response_normalizations = parse_and_validate_response(
                response.get("content", ""),
                schema,
                record["segment_id"],
                current_text,
                include_normalizations=True,
            )
            guarded, overrides = enforce_recall_routes(result, current_text)
            audit_attempt["validation"] = "passed"
            audit_attempt["response_normalizations"] = response_normalizations
            audit_attempt["route_overrides"] = overrides
            attempts.append(audit_attempt)
            return guarded, {
                "segment_id": record["segment_id"],
                "attempts": attempts,
            }
        except ResponseValidationError as exc:
            last_error = str(exc)
            audit_attempt["validation"] = "failed"
            audit_attempt["validation_error"] = last_error
            attempts.append(audit_attempt)
            if attempt_index < repair_attempts:
                current_prompt = build_repair_prompt(
                    user_prompt,
                    response.get("content", ""),
                    last_error,
                )

    raise ClassificationFailure(record["segment_id"], last_error, attempts)


def classify_records(
    records: list[dict],
    system_prompt: str,
    schema: dict,
    client: ModelClient,
    context_chars: int = 300,
    repair_attempts: int = 1,
) -> tuple[list[dict], list[dict], list[dict]]:
    """逐片段分类；失败项显式进入人工复核队列。"""
    results: list[dict] = []
    audit: list[dict] = []
    review_queue: list[dict] = []
    for window in build_context_records(records, context_chars):
        record = window["record"]
        try:
            result, item_audit = classify_one(
                record=record,
                context_before=window["context_before"],
                context_after=window["context_after"],
                system_prompt=system_prompt,
                schema=schema,
                client=client,
                repair_attempts=repair_attempts,
            )
            results.append(result)
            audit.append(item_audit)
        except ClassificationFailure as exc:
            audit.append({"segment_id": exc.segment_id, "attempts": exc.attempts})
            review_queue.append(
                {
                    "segment_id": exc.segment_id,
                    "route": "HUMAN_REVIEW",
                    "reason": "model_response_invalid_after_repair",
                    "error": exc.message,
                    "raw_text": record["raw_text"],
                    "clean_text": record["clean_text"],
                    "quality_flags": record.get("quality_flags", []),
                }
            )
    return results, audit, review_queue
