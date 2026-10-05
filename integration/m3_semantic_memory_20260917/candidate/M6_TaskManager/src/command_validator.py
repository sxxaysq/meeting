"""Convert model decisions into deterministic, executable task commands.

Structural integrity is still checked here and stays deterministic: the target
must be in the candidate set, its version must not have moved, the state machine
must allow the transition, evidence must be a literal substring of the source,
and the command must satisfy its JSON Schema.

What is no longer checked here is *wording*. Eleven regex predicates used to
decide whether evidence proved completion, negated a transfer, or authorised a
rename. Keyword lists cannot distinguish ``不得取消`` from ``取消`` or ``完成30%``
from ``完成``, so those readings moved to
:class:`M3_KnowledgeGraph.src.semantic_reader.SemanticReader` — one focused model
call per distinct (kind, evidence, task goal), remembered in the memory graph so
repeated wording across meetings costs nothing.

With no reader configured the semantic narrowing is skipped rather than guessed,
and the reason says so.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .candidate_retriever import normalize_text, project_compatible, same_goal
from .department_router import DepartmentRouter
from .models import (
    DecisionValidationError,
    BusinessAmbiguity,
    ExecutionConflict,
    LifecycleAction,
    LifecycleDecision,
    SourceContext,
    TASK_ITEM_TYPES,
    TaskCommand,
)
from .repository import TaskRepository
from .state_machine import next_status
from .input_policy import restriction, pending_restriction

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
        reader: Any | None = None,
        default_department: str | None = None,
    ) -> None:
        self.repository = repository
        self.router = router
        self.reader = reader
        # Company-level directives (item_type=NON_PROJECT_WORK) legitimately have
        # no single responsible department, so M1 extracts department=null. A CREATE
        # still needs one deterministic route: fall back to this Department Master
        # entry (never invented — it must exist in the master) and mark the change.
        if default_department is None:
            default_department = os.getenv("M6_DEFAULT_DEPARTMENT", "公司级")
        self.default_department = default_department or None
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
        except BusinessAmbiguity as error:
            command = self._review_command(
                source,
                candidates,
                f"BUSINESS_REVIEW: {error}",
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

    def build_multi(
        self,
        source: SourceContext,
        decision: LifecycleDecision,
        candidates: list[dict[str, Any]],
    ) -> list[TaskCommand]:
        """Validate each sub-decision of a MULTI decision like a single decision.

        All-or-nothing: a BusinessAmbiguity (or a policy-forced review) inside any
        sub-decision collapses the whole item into one review command, so a
        multi-goal item is never partially executed.
        """
        subs = list(decision.sub_decisions)
        if len(subs) < 2:
            raise DecisionValidationError("MULTI 至少需要两条子决策")
        try:
            commands = [self._build(source, sub, candidates) for sub in subs]
        except BusinessAmbiguity as error:
            command = self._review_command(
                source,
                candidates,
                f"BUSINESS_REVIEW: {error}",
            )
            self._validate_schema(command)
            return [command]
        reviews = [command for command in commands if command.action is LifecycleAction.REVIEW]
        if reviews:
            command = self._review_command(
                source,
                candidates,
                "MULTI 子决策触发复核：" + reviews[0].reason,
            )
            self._validate_schema(command)
            return [command]
        stamped = [
            replace(
                command,
                provenance={
                    **command.provenance,
                    "multi_goal_index": position,
                    "multi_goal_total": len(commands),
                },
            )
            for position, command in enumerate(commands)
        ]
        for command in stamped:
            self._validate_schema(command)
        return stamped

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
        blocked = restriction(source, action.value) or pending_restriction(source, self.repository, action.value)
        if blocked:
            return self._review_command(source, candidates, blocked)
        if action is LifecycleAction.REVIEW:
            if decision.target_task_id is not None:
                raise DecisionValidationError("REVIEW 不允许锁定目标任务")
            return self._review_command(source, candidates, decision.reason)
        if action is LifecycleAction.CREATE:
            if decision.target_task_id is not None:
                raise DecisionValidationError("CREATE 不允许指定目标任务")
            if any(same_goal(item,c) for c in candidates if project_compatible(source,c) or c.get('identity_uncertain')):
                raise BusinessAmbiguity('DUPLICATE_GOAL: 具体业务目标已存在，不能重复新建')
            route, fallback = self._route_for_create(item)
            changes = self._create_changes(source, route)
            if fallback:
                changes["department_fallback"] = route["name"]
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
        if candidate.get('identity_uncertain'):
            raise BusinessAmbiguity('PROJECT_IDENTITY: 仅召回了疑似同目标，项目身份未确认，暂缓该次操作')
        current = self.repository.get_task(target_id)
        if current is None:
            raise DecisionValidationError("目标任务不存在")
        if current["version"] != candidate["version"]:
            raise ExecutionConflict("目标任务版本已变化")
        self._validate_project(source, current)
        if decision.scope == 'uncertain':
            raise BusinessAmbiguity('TARGET_SCOPE: 无法确认该目标的业务身份')
        if action is LifecycleAction.MODIFY and decision.scope=='subtask':
            decision=replace(decision,decision=LifecycleAction.PROGRESS_UPDATE,changes={},
                             reason=decision.reason+'；子事项只追加进展，不修改父任务定义')
            action=decision.decision
        repeated = normalize_text(item['content']) in {
            normalize_text(current['description']),
            *(normalize_text(e['content']) for e in candidate.get('recent_events',[]))}
        if action is LifecycleAction.SKIP:
            if repeated:
                return replace(self.skip(source),reason='同一目标的完全重复事实；'+decision.reason)
            decision=replace(decision,decision=LifecycleAction.PROGRESS_UPDATE,
                             reason=decision.reason+'；原文不是完全重复，保留为新进展')
            action=decision.decision
        if action is LifecycleAction.COMPLETE:
            evidence=decision.completion_evidence or ''
            if evidence and evidence not in item['content'] and evidence not in item['evidence']['text']:
                raise DecisionValidationError('COMPLETE evidence 不是当前来源原文')
            # Only an explicit whole-goal completion may close a task; a partial or
            # future statement stays a progress update. The reading is the model's.
            surrounding=self._evidence_context(source,evidence)
            parent_task=normalize_text(current['title'])==normalize_text(current.get('project'))
            parent_mismatch=parent_task and normalize_text(item['title'])!=normalize_text(current['title'])
            reading=self._read_completion(source,evidence,surrounding,current)
            if reading is None:
                unproven=decision.scope!='same_task' or parent_mismatch
                note='未配置语义判读器，仅按范围与目标一致性收窄'
            else:
                unproven=(decision.scope!='same_task' or parent_mismatch
                    or not reading['definite'] or reading['future_or_partial']
                    or not reading['whole_goal'] or not reading['phases_covered']
                    or not reading['goal_in_evidence'])
                note=reading.get('reason') or ''
            if unproven:
                decision=replace(decision,decision=LifecycleAction.PROGRESS_UPDATE,
                    reason=(decision.reason+'；未证明整个目标完成，保留为进展'
                            +(('；'+note) if note else '')))
                action=decision.decision
        if action in (LifecycleAction.CANCEL,LifecycleAction.REOPEN):
            evidence=decision.completion_evidence or ''
            if not evidence or (evidence not in item['content'] and evidence not in item['evidence']['text']):
                raise DecisionValidationError(action.value+' 缺少原文明示的状态变更依据')
            reading=self._read_status(action.value,source,evidence,current)
            if reading is not None and (reading['negated'] or not reading['explicit']):
                raise DecisionValidationError(
                    action.value+' 原文没有明示该状态变更'+
                    (('；'+reading['reason']) if reading.get('reason') else ''))
        next_status(current["status"], action)

        changes: dict[str, Any] = {}
        department_change = None
        if action is LifecycleAction.MODIFY:
            changes = self._grounded_changes(source, decision.changes)
            # Additive changes preserve valid history. Reporting labels do not replace task identity.
            if 'description' in changes:
                incoming=changes['description']
                changes['description']=current['description'] if normalize_text(incoming) in normalize_text(current['description']) else current['description']+'\n'+incoming
            if 'assignees' in changes:
                changes['assignees']=list(dict.fromkeys(current.get('assignees',[])+changes['assignees']))
            if any(field in changes for field in ('title','work_section','delivery_group')):
                # Identity fields move only on an explicit renaming statement.
                rename=self._read_rename(source,item['content'],current)
                if rename is not None and not rename['explicit_rename']:
                    for field in ('title','work_section','delivery_group'):
                        changes.pop(field,None)
            changes={k:v for k,v in changes.items() if v != current.get(k)}
            if not changes:
                if repeated:
                    return replace(self.skip(source),reason='MODIFY 无字段变化且事实重复；'+decision.reason)
                decision=replace(decision,decision=LifecycleAction.PROGRESS_UPDATE,
                    reason=decision.reason+'；无稳定字段变化，追加进展')
            route_name = current.get("department")
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
            route_name = current.get("department")
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
        if target and (source.admission or {}).get('scope_validated'):
            # M6 project assignment is program-resolved, never an arbitrary model field.
            changes = {**changes,'project':source.item.get('project'),'project_entity_id':source.project_entity_id}
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
            "source_project": (source.admission or {}).get('original_item',item).get('project'),
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
        if not project_compatible(source,target):
            raise DecisionValidationError('项目身份不兼容：记忆库解析出的项目族不同')

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
        reading = self._read_status("TRANSFER", source, evidence, target)
        if reading is not None and (reading["negated"] or not reading["explicit"]):
            raise DecisionValidationError(
                "TRANSFER 原文没有明确移交语义"
                + (("；" + reading["reason"]) if reading.get("reason") else "")
            )
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
            raise BusinessAmbiguity(
                f"部门无法唯一映射到 Department Master：{department!r}"
            )
        return route

    def _route_for_create(self, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        department = item.get("department")
        if (
            not str(department or "").strip()
            and item.get("item_type") == "NON_PROJECT_WORK"
            and self.default_department
        ):
            return self._route(self.default_department), True
        return self._route(department), False

    @staticmethod
    def _evidence_context(source,evidence):
        """Include immediate negation/condition prefix; a quote cannot trim away 不/未."""
        if not evidence:return ''
        for text in (source.item['content'],source.item['evidence']['text']):
            position=text.find(evidence)
            if position>=0:return text[max(0,position-6):position+len(evidence)]
        return evidence

    # ------------------------------------------------------------ semantic reads

    def _read_completion(
        self,
        source: SourceContext,
        evidence: str,
        surrounding: str,
        current: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.reader is None:
            return None
        return self.reader.read_completion(
            evidence=evidence,
            context=surrounding,
            task_title=str(current.get("title") or ""),
            task_description=str(current.get("description") or ""),
            task_project=str(current.get("project") or ""),
        )

    def _read_status(
        self,
        kind: str,
        source: SourceContext,
        evidence: str,
        current: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.reader is None:
            return None
        return self.reader.read_status_change(
            kind,
            evidence=evidence,
            context=self._evidence_context(source, evidence),
            task_title=str(current.get("title") or ""),
        )

    def _read_rename(
        self, source: SourceContext, evidence: str, current: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self.reader is None:
            return None
        return self.reader.read_rename(
            evidence=evidence,
            context=self._evidence_context(source, evidence),
            task_title=str(current.get("title") or ""),
        )

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
