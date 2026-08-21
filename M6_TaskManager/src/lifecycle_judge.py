"""LLM lifecycle judge constrained to program-retrieved candidates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .llm_client import LifecycleClient
from .models import LifecycleDecision, SourceContext


class LifecycleJudge:
    def __init__(self, client: LifecycleClient) -> None:
        root = Path(__file__).resolve().parents[1]
        self.client = client
        self.system_prompt = (
            root / "prompts" / "lifecycle_judge.md"
        ).read_text(encoding="utf-8")
        schema = json.loads(
            (root / "schemas" / "lifecycle_decision.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.validator = Draft202012Validator(schema)

    def judge(
        self,
        source: SourceContext,
        candidates: list[dict[str, Any]],
    ) -> tuple[LifecycleDecision, dict[str, Any]]:
        payload = self._payload(source, candidates)
        decision_payload, audit = self.client.decide(
            self.system_prompt, payload
        )
        decision_payload = self._normalize_shape(decision_payload)
        errors = self._errors(decision_payload)
        if errors:
            decision_payload, retry_audit = self.client.decide(
                self.system_prompt,
                payload,
                correction="; ".join(errors),
            )
            decision_payload = self._normalize_shape(decision_payload)
            retry_errors = self._errors(decision_payload)
            if retry_errors:
                raise ValueError(
                    "生命周期 JSON 校验失败：" + "; ".join(retry_errors)
                )
            audit["format_retry"] = retry_audit
        return LifecycleDecision.from_dict(decision_payload), audit

    @staticmethod
    def _normalize_shape(payload: dict[str, Any]) -> dict[str, Any]:
        """Accept two common JSON-mode wrappers without changing semantics."""
        if set(payload) == {"lifecycle_decision"} and isinstance(
            payload["lifecycle_decision"], dict
        ):
            payload = dict(payload["lifecycle_decision"])
        if payload.get("department_change") == {}:
            payload["department_change"] = None
        payload.setdefault("event_summary", None)
        payload.setdefault("changes", {})
        payload.setdefault("department_change", None)
        return payload

    def _errors(self, payload: dict[str, Any]) -> list[str]:
        return [
            f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: "
            f"{error.message}"
            for error in sorted(
                self.validator.iter_errors(payload),
                key=lambda error: list(error.absolute_path),
            )[:5]
        ]

    @staticmethod
    def _payload(
        source: SourceContext,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        candidate_fields = (
            "task_id",
            "item_type",
            "project_entity_id",
            "project",
            "department",
            "work_section",
            "delivery_group",
            "title",
            "description",
            "assignees",
            "status",
            "version",
            "created_at",
            "updated_at",
            "recent_events",
        )
        return {
            "current_item": {
                **source.item,
                "canonical_project_entity_id": source.project_entity_id,
                "source_document_id": source.source_document_id,
                "source_item_id": source.source_item_id,
            },
            "historical_candidates": [
                {field: candidate.get(field) for field in candidate_fields}
                for candidate in candidates
            ],
            "allowed_decisions": [
                "CREATE",
                "PROGRESS_UPDATE",
                "MODIFY",
                "COMPLETE",
                "CANCEL",
                "REOPEN",
                "TRANSFER",
                "REVIEW",
            ],
            "allowed_grounded_changes": {
                "title": source.item.get("title"),
                "description": source.item.get("content"),
                "work_section": source.item.get("work_section"),
                "delivery_group": source.item.get("delivery_group"),
                "assignees": source.item.get("assignee") or [],
            },
            "required_output_shape": {
                "decision": "CREATE|PROGRESS_UPDATE|MODIFY|COMPLETE|CANCEL|REOPEN|TRANSFER|REVIEW",
                "target_task_id": None,
                "reason": "简短可审计理由",
                "event_summary": None,
                "changes": {},
                "department_change": None,
            },
            "output_warning": (
                "直接输出 required_output_shape 对象本身；"
                "禁止增加 lifecycle_decision 等外层包装。"
            ),
        }
