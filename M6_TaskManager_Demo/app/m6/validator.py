"""M6 命令的 Schema、证据、目标和动作边界校验。"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


ALLOWED_PATCH_FIELDS = {
    "title",
    "description",
    "work_items",
    "assignee_raw",
    "deadline_raw",
    "status",
}
UPDATE_ACTIONS = {"UPDATE_FIELDS", "UPDATE_STATUS", "SOFT_DELETE"}


class CommandValidationError(ValueError):
    """M6 命令不满足确定性安全边界。"""


class CommandValidator:
    def __init__(self, schema_path: Path | str | None = None) -> None:
        path = Path(schema_path or Path(__file__).with_name("m6_command.schema.json"))
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.validator = Draft202012Validator(schema)

    def validate_batch(
        self,
        batch: dict,
        source: dict,
        meeting_id: str,
        match_context: dict,
    ) -> dict:
        errors = sorted(
            self.validator.iter_errors(batch),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            message = "; ".join(
                f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: "
                f"{error.message}"
                for error in errors[:5]
            )
            raise CommandValidationError(f"M6 Schema 校验失败：{message}")

        expected_source_key = (
            f"{meeting_id}:{source['segment_id']}:{source['subsegment_id']}"
        )
        if batch["source_key"] != expected_source_key:
            raise CommandValidationError("source_key 与当前来源不一致")

        candidates = {
            item["task_id"]: item for item in match_context.get("candidates", [])
        }
        unique_target = match_context.get("unique_target_task_id")
        indexes = [command["command_index"] for command in batch["commands"]]
        if len(indexes) != len(set(indexes)):
            raise CommandValidationError("command_index 重复")

        for command in batch["commands"]:
            self._validate_command(
                command,
                source=source,
                candidates=candidates,
                unique_target=unique_target,
            )
        return batch

    def _validate_command(
        self,
        command: dict,
        source: dict,
        candidates: dict,
        unique_target: str | None,
    ) -> None:
        action = command["db_action"]
        target_task_id = command["target_task_id"]
        expected_version = command["expected_version"]
        patch = command["task_patch"]
        evidence = command["evidence"]

        if set(patch) != ALLOWED_PATCH_FIELDS:
            raise CommandValidationError("task_patch 字段不符合白名单")
        for field, value in patch.items():
            if isinstance(value, str) and not value.strip():
                raise CommandValidationError(
                    f"task_patch.{field} 不允许为空字符串"
                )
        work_items = patch.get("work_items")
        if work_items is not None and any(
            not isinstance(item, str) or not item.strip()
            for item in work_items
        ):
            raise CommandValidationError(
                "task_patch.work_items 只允许非空字符串"
            )
        if (
            evidence["segment_id"] != source["segment_id"]
            or evidence["subsegment_id"] != source["subsegment_id"]
        ):
            raise CommandValidationError("证据来源 ID 与当前 M2 记录不一致")
        if evidence["text"] not in source["text"]:
            raise CommandValidationError("证据文本不是当前 M2 原文的逐字片段")

        if action == "CREATE":
            if target_task_id is not None or expected_version is not None:
                raise CommandValidationError("CREATE 不允许指定目标任务或版本")
            if not patch.get("title") or not patch["title"].strip():
                raise CommandValidationError("CREATE 必须包含非空 title")
            if patch.get("status") not in {None, "open", "in_progress"}:
                raise CommandValidationError("CREATE 初始状态只能是 open/in_progress")
        elif action in UPDATE_ACTIONS:
            if not unique_target:
                raise CommandValidationError("没有唯一目标时禁止自动删改")
            if target_task_id != unique_target:
                raise CommandValidationError("M6 目标不是确定性匹配出的唯一任务")
            candidate = candidates.get(target_task_id)
            if candidate is None:
                raise CommandValidationError("目标任务不在允许候选集合")
            if expected_version != candidate["version"]:
                raise CommandValidationError("expected_version 与候选版本不一致")
            if action == "UPDATE_FIELDS":
                changed = {
                    field
                    for field, value in patch.items()
                    if value is not None and field != "status"
                }
                if not changed or patch.get("status") is not None:
                    raise CommandValidationError(
                        "UPDATE_FIELDS 必须修改至少一个非状态字段"
                    )
            elif action == "UPDATE_STATUS":
                if patch.get("status") not in {
                    "open",
                    "in_progress",
                    "blocked",
                    "completed",
                }:
                    raise CommandValidationError("UPDATE_STATUS 状态无效")
                if any(
                    value is not None
                    for field, value in patch.items()
                    if field != "status"
                ):
                    raise CommandValidationError(
                        "UPDATE_STATUS 不允许同时修改其他字段"
                    )
            elif action == "SOFT_DELETE":
                if any(value is not None for value in patch.values()):
                    raise CommandValidationError(
                        "SOFT_DELETE 的 task_patch 必须全部为空"
                    )
        else:
            if target_task_id is not None or expected_version is not None:
                raise CommandValidationError("NOOP 不允许指定目标任务或版本")
            if any(value is not None for value in patch.values()):
                raise CommandValidationError("NOOP 的 task_patch 必须全部为空")
