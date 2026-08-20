"""Convert model decisions into deterministic, executable task commands."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .candidate_retriever import normalize_text
from .department_router import DepartmentRouter
from .models import (
    DecisionValidationError,
    LifecycleAction,
    LifecycleDecision,
    SourceContext,
    TASK_ITEM_TYPES,
    TaskCommand,
)
from .repository import TaskRepository
from .state_machine import next_status


TRANSFER_PATTERN = re.compile(
    r"(?:移交|转交|改由|改为|由.{1,30}(?:接手|负责|牵头))"
)
CHANGE_FIELDS = {
    "title",
    "description",
    "work_section",
    "delivery_group",
    "assignees",
}


class CommandValidator:
    def __init__(
        self,
        repository: TaskRepository,
        router: DepartmentRouter,
    ) -> None:
        self.repository = repository
        self.router = router
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "task_command.schema.json"
        )
        self.schema_validator = Draft202012Validator(
            json.loads(schema_path.read_text(encoding="utf-8"))
        )

    def build(
        self,
        source: SourceContext,
        decision: LifecycleDecision,
        candidates: list[dict[str, Any]],
    ) -> TaskCommand:
        try:
            command = self._build(source, decision, candidates)
            self._validate_schema(command)
            return command
        except DecisionValidationError as error:
            command = self._review_command(
                source,
                candidates,
                f"DETERMINISTIC_SAFETY_REVIEW: {error}",
            )
            self._validate_schema(command)
            return command

    def skip(self, source: SourceContext) -> TaskCommand:
        command = TaskCommand(
            action=LifecycleAction.SKIP,
            target_task_id=None,
            expected_version=None,
            source_document_id=source.source_document_id,
            source_item_id=source.source_item_id,
            event_content=source.item["content"],
            reason="NON_TASK_ITEM 默认不进入历史任务生命周期",
            changes={},
            department_change=None,
            provenance=self._provenance(source, []),
        )
        self._validate_schema(command)
        return command

    def review_for_failure(
        self,
        source: SourceContext,
        candidates: list[dict[str, Any]],
        reason: str,
    ) -> TaskCommand:
        command = self._review_command(source, candidates, reason)
        self._validate_schema(command)
        return command

    def _build(
        self,
        source: SourceContext,
        decision: LifecycleDecision,
        candidates: list[dict[str, Any]],
    ) -> TaskCommand:
        item = source.item
        if item["item_type"] not in TASK_ITEM_TYPES:
            raise DecisionValidationError("非任务 Item 不允许调用生命周期动作")
        candidate_by_id = {
            candidate["task_id"]: candidate for candidate in candidates
        }
        action = decision.decision
        if action is LifecycleAction.REVIEW:
            if decision.target_task_id is not None:
                raise DecisionValidationError("REVIEW 不允许锁定目标任务")
            return self._review_command(source, candidates, decision.reason)
        if action is LifecycleAction.CREATE:
            if decision.target_task_id is not None:
                raise DecisionValidationError("CREATE 不允许指定目标任务")
            route = self._route(item.get("department"))
            changes = self._create_changes(source, route)
            return self._command(
                source,
                candidates,
                decision,
                target=None,
                expected_version=None,
                changes=changes,
            )

        target_id = decision.target_task_id
        if not target_id or target_id not in candidate_by_id:
            raise DecisionValidationError("目标任务不在本次候选集合")
        candidate = candidate_by_id[target_id]
        current = self.repository.get_task(target_id)
        if current is None:
            raise DecisionValidationError("目标任务不存在")
        if current["version"] != candidate["version"]:
            raise DecisionValidationError("目标任务版本已变化")
        self._validate_project(source, current)
        self._validate_department_consistency(source, current, action)
        next_status(current["status"], action)

        changes: dict[str, Any] = {}
        department_change = None
        if action is LifecycleAction.MODIFY:
            changes = self._grounded_changes(source, decision.changes)
            if not changes:
                raise DecisionValidationError("MODIFY 没有有依据的稳定字段变化")
            route_name = item.get("department") or current.get("department")
            route = self._route(route_name)
            changes.update(
                {
                    "department_id": route["department_id"],
                    "department": route["name"],
                    "department_route": route["route"],
                }
            )
        elif action is LifecycleAction.TRANSFER:
            department_change, route = self._validate_transfer(
                source, current, decision.department_change
            )
            changes = {
                "department_id": route["department_id"],
                "department": route["name"],
                "department_route": route["route"],
            }
        else:
            route_name = item.get("department") or current.get("department")
            route = self._route(route_name)
            changes = {
                "department_id": route["department_id"],
                "department": route["name"],
                "department_route": route["route"],
            }
        return self._command(
            source,
            candidates,
            decision,
            target=target_id,
            expected_version=current["version"],
            changes=changes,
            department_change=department_change,
        )

    def _command(
        self,
        source: SourceContext,
        candidates: list[dict[str, Any]],
        decision: LifecycleDecision,
        *,
        target: str | None,
        expected_version: int | None,
        changes: dict[str, Any],
        department_change: dict[str, Any] | None = None,
    ) -> TaskCommand:
        return TaskCommand(
            action=decision.decision,
            target_task_id=target,
            expected_version=expected_version,
            source_document_id=source.source_document_id,
            source_item_id=source.source_item_id,
            event_content=source.item["content"],
            reason=decision.reason,
            changes=changes,
            department_change=department_change,
            provenance=self._provenance(source, candidates),
        )

    def _review_command(
        self,
        source: SourceContext,
        candidates: list[dict[str, Any]],
        reason: str,
    ) -> TaskCommand:
        return TaskCommand(
            action=LifecycleAction.REVIEW,
            target_task_id=None,
            expected_version=None,
            source_document_id=source.source_document_id,
            source_item_id=source.source_item_id,
            event_content=source.item["content"],
            reason=reason[:1000],
            changes={},
            department_change=None,
            provenance=self._provenance(source, candidates),
        )

    def _create_changes(
        self,
        source: SourceContext,
        route: dict[str, Any],
    ) -> dict[str, Any]:
        item = source.item
        return {
            "item_type": item["item_type"],
            "project_entity_id": source.project_entity_id,
            "project": item.get("project"),
            "department_id": route["department_id"],
            "department": route["name"],
            "department_route": route["route"],
            "work_section": item.get("work_section"),
            "delivery_group": item.get("delivery_group"),
            "title": item["title"],
            "description": item["content"],
            "assignees": item.get("assignee") or [],
            "status": "OPEN",
        }

    def _grounded_changes(
        self,
        source: SourceContext,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        unknown = set(changes) - CHANGE_FIELDS
        if unknown:
            raise DecisionValidationError(
                f"changes 包含不允许字段：{sorted(unknown)}"
            )
        item = source.item
        grounded: dict[str, Any] = {}
        field_sources = {
            "title": item.get("title"),
            "description": item.get("content"),
            "work_section": item.get("work_section"),
            "delivery_group": item.get("delivery_group"),
            "assignees": item.get("assignee") or [],
        }
        for field, value in changes.items():
            if value is None:
                continue
            expected = field_sources[field]
            if field == "assignees":
                if not isinstance(value, list) or not set(value).issubset(
                    set(expected)
                ):
                    raise DecisionValidationError("assignees 缺少当前 Item 依据")
            elif normalize_text(value) != normalize_text(expected):
                raise DecisionValidationError(f"{field} 缺少当前 Item 依据")
            grounded[field] = value
        return grounded

    def _validate_project(
        self,
        source: SourceContext,
        target: dict[str, Any],
    ) -> None:
        if source.item["item_type"] != "PROJECT_TASK":
            return
        if source.project_entity_id and target.get("project_entity_id"):
            if source.project_entity_id != target["project_entity_id"]:
                raise DecisionValidationError("PROJECT_TASK 项目实体冲突")
            return
        current_project = normalize_text(source.item.get("project"))
        target_project = normalize_text(target.get("project"))
        if not current_project or not target_project:
            raise DecisionValidationError("PROJECT_TASK 项目信息不足")
        if current_project != target_project:
            raise DecisionValidationError("PROJECT_TASK canonical project 冲突")

    def _validate_department_consistency(
        self,
        source: SourceContext,
        target: dict[str, Any],
        action: LifecycleAction,
    ) -> None:
        if action is LifecycleAction.TRANSFER:
            return
        current_department = normalize_text(source.item.get("department"))
        target_department = normalize_text(target.get("department"))
        if (
            current_department
            and target_department
            and current_department != target_department
        ):
            current_route = self.router.resolve(source.item.get("department"))
            target_route = self.router.resolve(target.get("department"))
            if (
                current_route
                and target_route
                and current_route["department_id"]
                == target_route["department_id"]
            ):
                return
            raise DecisionValidationError("部门冲突且没有明确 TRANSFER")

    def _validate_transfer(
        self,
        source: SourceContext,
        target: dict[str, Any],
        change: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not change:
            raise DecisionValidationError("TRANSFER 缺少 department_change")
        evidence = str(change.get("evidence") or "").strip()
        content = source.item["content"]
        evidence_text = str(source.item.get("evidence", {}).get("text") or "")
        if not evidence or (evidence not in content and evidence not in evidence_text):
            raise DecisionValidationError("TRANSFER evidence 不是原文逐字片段")
        if not TRANSFER_PATTERN.search(evidence):
            raise DecisionValidationError("TRANSFER 原文没有明确移交语义")
        to_department = str(change.get("to_department") or "").strip()
        if not to_department or normalize_text(to_department) not in normalize_text(
            evidence
        ):
            raise DecisionValidationError("TRANSFER 目标部门缺少原文依据")
        from_department = change.get("from_department")
        if (
            from_department
            and target.get("department")
            and normalize_text(from_department)
            != normalize_text(target["department"])
        ):
            raise DecisionValidationError("TRANSFER 原部门与历史任务不一致")
        route = self._route(to_department)
        return {
            "from_department": target.get("department"),
            "to_department": route["name"],
            "evidence": evidence,
        }, route

    def _route(self, department: str | None) -> dict[str, Any]:
        route = self.router.resolve(department)
        if route is None:
            raise DecisionValidationError(
                f"部门无法唯一映射到 Department Master：{department!r}"
            )
        return route

    @staticmethod
    def _provenance(
        source: SourceContext,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            **source.provenance,
            "item": source.item,
            "candidate_task_ids": [
                candidate["task_id"] for candidate in candidates
            ],
            "candidates": candidates,
        }

    def _validate_schema(self, command: TaskCommand) -> None:
        errors = sorted(
            self.schema_validator.iter_errors(command.as_dict()),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            message = "; ".join(
                f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: "
                f"{error.message}"
                for error in errors[:5]
            )
            raise DecisionValidationError(f"TaskCommand Schema 失败：{message}")
