"""将 M2 记录转换为 M6 命令并安全落库的应用服务。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ..task_repository import TaskRepository
from .executor import CommandExecutor
from .granularity import normalize_task_granularity
from .model_client import M6Client, empty_patch
from .target_matcher import TargetMatcher
from .validator import CommandValidationError, CommandValidator


M2_REQUIRED_FIELDS = {
    "segment_id",
    "subsegment_id",
    "speaker_id",
    "speaker_confidence",
    "start_ms",
    "end_ms",
    "text_raw",
    "asr_confidence",
    "nbest",
    "missing_fields",
    "task_category",
}
M2_CATEGORIES = {
    "task",
    "task_update",
    "decision",
    "issue",
    "report",
    "suggestion",
    "non_task",
}


def atomic_write_json(data: Any, path: Path | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def load_m2_records(path: Path | str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("M2 输出必须是 JSON 数组")
    seen: set[tuple[str, str]] = set()
    validated = []
    for index, record in enumerate(data):
        if not isinstance(record, dict):
            raise ValueError(f"M2 第 {index} 项不是对象")
        fields = set(record)
        if fields != M2_REQUIRED_FIELDS:
            missing = sorted(M2_REQUIRED_FIELDS - fields)
            extra = sorted(fields - M2_REQUIRED_FIELDS)
            raise ValueError(
                f"M2 第 {index} 项字段不符合 11 字段契约；"
                f"缺失={missing}，额外={extra}"
            )
        key = (record["segment_id"], record["subsegment_id"])
        if key in seen:
            raise ValueError(f"M2 存在重复复合 ID：{key[0]}/{key[1]}")
        seen.add(key)
        if (
            not isinstance(record["segment_id"], str)
            or not record["segment_id"]
            or not isinstance(record["subsegment_id"], str)
            or len(record["subsegment_id"]) != 3
            or not record["subsegment_id"].isdigit()
            or not isinstance(record["text_raw"], str)
            or not record["text_raw"].strip()
        ):
            raise ValueError(f"M2 第 {index} 项关键字段无效")
        if record["task_category"] not in M2_CATEGORIES:
            raise ValueError(f"M2 第 {index} 项 task_category 无效")
        validated.append(record)
    return validated


def _noop_batch(
    meeting_id: str,
    source: dict,
    reason_code: str,
    ambiguity: str,
) -> dict:
    source_key = (
        f"{meeting_id}:{source['segment_id']}:{source['subsegment_id']}"
    )
    return {
        "schema_version": "m6_db_command_v1",
        "source_key": source_key,
        "commands": [
            {
                "command_index": 1,
                "db_action": "NOOP",
                "target_task_id": None,
                "expected_version": None,
                "task_patch": empty_patch(),
                "evidence": {
                    "segment_id": source["segment_id"],
                    "subsegment_id": source["subsegment_id"],
                    "text": source["text"],
                },
                "reason_code": reason_code,
                "ambiguities": [ambiguity],
            }
        ],
    }


def _ensure_work_items_contract(batch: dict) -> dict:
    """兼容尚未输出 work_items 的旧版 m6_db_command_v1 客户端。"""
    for command in batch.get("commands", []):
        patch = command.get("task_patch")
        if isinstance(patch, dict):
            patch.setdefault("work_items", None)
    return batch


class TaskAutomationService:
    def __init__(
        self,
        repository: TaskRepository,
        client: M6Client,
        matcher: TargetMatcher,
        validator: CommandValidator,
        executor: CommandExecutor,
        allow_decision_create: bool = False,
    ) -> None:
        self.repository = repository
        self.client = client
        self.matcher = matcher
        self.validator = validator
        self.executor = executor
        self.allow_decision_create = allow_decision_create

    def process_records(
        self,
        records: list[dict],
        run_id: str,
        meeting: dict,
        output_dir: Path | str,
    ) -> dict:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        category_counts = Counter(
            record["task_category"] for record in records
        )
        command_batches: list[dict] = []
        audit_records: list[dict] = []
        validation_failures: list[dict] = []
        execution_results: list[dict] = []

        for record in records:
            category = record["task_category"]
            if category not in {"task", "task_update", "decision"}:
                continue
            if category == "decision" and not self.allow_decision_create:
                continue

            source = {
                "segment_id": record["segment_id"],
                "subsegment_id": record["subsegment_id"],
                "actor_hint": record["speaker_id"],
                "speaker_confidence": record["speaker_confidence"],
                "text": record["text_raw"],
                "task_category": category,
            }
            match_context = self.matcher.match(
                source["text"],
                self.repository.get_active_tasks(),
            )

            if category == "task_update" and not match_context.get(
                "unique_target_task_id"
            ):
                batch = _noop_batch(
                    meeting["meeting_id"],
                    source,
                    "NOOP_UNMATCHED",
                    "没有唯一目标任务，未自动修改数据库",
                )
                model_audit = {
                    "model": "deterministic-noop",
                    "usage": {},
                    "match_mode": match_context["match_mode"],
                }
            else:
                try:
                    batch, model_audit = self.client.generate(
                        meeting, source, match_context
                    )
                except Exception as model_error:
                    failure = {
                        "source": source,
                        "match_context": match_context,
                        "first_error": "M6 模型调用失败",
                        "final_error": str(model_error),
                    }
                    validation_failures.append(failure)
                    source_key = (
                        f"{meeting['meeting_id']}:{source['segment_id']}:"
                        f"{source['subsegment_id']}"
                    )
                    self.repository.record_failed_event(
                        event_key=f"{source_key}:model",
                        run_id=run_id,
                        evidence_text=source["text"],
                        failure_reason=str(model_error),
                        db_action="M6_MODEL_FAILURE",
                    )
                    continue

            batch = _ensure_work_items_contract(batch)
            try:
                self.validator.validate_batch(
                    batch,
                    source=source,
                    meeting_id=meeting["meeting_id"],
                    match_context=match_context,
                )
            except CommandValidationError as first_error:
                try:
                    batch, retry_audit = self.client.generate(
                        meeting,
                        source,
                        match_context,
                        correction=str(first_error),
                    )
                    batch = _ensure_work_items_contract(batch)
                    self.validator.validate_batch(
                        batch,
                        source=source,
                        meeting_id=meeting["meeting_id"],
                        match_context=match_context,
                    )
                    model_audit["retry"] = retry_audit
                except Exception as second_error:
                    failure = {
                        "source": source,
                        "match_context": match_context,
                        "first_error": str(first_error),
                        "final_error": str(second_error),
                    }
                    validation_failures.append(failure)
                    source_key = (
                        f"{meeting['meeting_id']}:{source['segment_id']}:"
                        f"{source['subsegment_id']}"
                    )
                    self.repository.record_failed_event(
                        event_key=f"{source_key}:validation",
                        run_id=run_id,
                        evidence_text=source["text"],
                        failure_reason=str(second_error),
                    )
                    continue

            batch, granularity_audit = normalize_task_granularity(batch, source)
            try:
                self.validator.validate_batch(
                    batch,
                    source=source,
                    meeting_id=meeting["meeting_id"],
                    match_context=match_context,
                )
            except CommandValidationError as granularity_error:
                failure = {
                    "source": source,
                    "match_context": match_context,
                    "first_error": "任务粒度归并后校验失败",
                    "final_error": str(granularity_error),
                }
                validation_failures.append(failure)
                source_key = (
                    f"{meeting['meeting_id']}:{source['segment_id']}:"
                    f"{source['subsegment_id']}"
                )
                self.repository.record_failed_event(
                    event_key=f"{source_key}:granularity",
                    run_id=run_id,
                    evidence_text=source["text"],
                    failure_reason=str(granularity_error),
                    db_action="M6_GRANULARITY_FAILURE",
                )
                continue

            command_batches.append(batch)
            results = self.executor.execute_batch(
                batch,
                run_id=run_id,
                meeting_id=meeting["meeting_id"],
            )
            execution_results.extend(results)
            audit_records.append(
                {
                    "source": source,
                    "match_context": match_context,
                    "model": model_audit,
                    "granularity": granularity_audit,
                    "commands": batch,
                    "execution_results": results,
                }
            )

        atomic_write_json(command_batches, output_path / "commands.json")
        atomic_write_json(audit_records, output_path / "model_audit.json")
        atomic_write_json(
            validation_failures,
            output_path / "validation_failures.json",
        )

        result_counts = Counter(
            result["execution_status"] for result in execution_results
        )
        action_counts = Counter(
            result["db_action"]
            for result in execution_results
            if result["execution_status"] == "applied"
        )
        return {
            "m2_category_counts": dict(category_counts),
            "command_batch_count": len(command_batches),
            "command_count": len(execution_results) + len(validation_failures),
            "applied_count": result_counts["applied"],
            "noop_count": result_counts["noop"]
            + result_counts["duplicate"],
            "failed_count": result_counts["failed"]
            + len(validation_failures),
            "action_counts": dict(action_counts),
            "validation_failure_count": len(validation_failures),
        }
