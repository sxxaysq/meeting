"""M2 PASS item to safe lifecycle execution application service."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .candidate_retriever import CandidateRetriever
from .command_validator import CommandValidator
from .executor import TaskExecutor
from .lifecycle_judge import LifecycleJudge
from .m2_input import load_m2_payload
from .models import ExecutionConflict, SourceContext
from .repository import TaskRepository


def atomic_write_json(path: Path | str, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


class TaskLifecycleService:
    def __init__(
        self,
        *,
        repository: TaskRepository,
        retriever: CandidateRetriever,
        judge: LifecycleJudge,
        validator: CommandValidator,
        executor: TaskExecutor,
    ) -> None:
        self.repository = repository
        self.retriever = retriever
        self.judge = judge
        self.validator = validator
        self.executor = executor

    def process_context(self, source: SourceContext) -> dict[str, Any]:
        existing = self.repository.get_processing_record(
            source.source_document_id, source.source_item_id
        )
        if existing:
            return {
                "source_document_id": source.source_document_id,
                "source_item_id": source.source_item_id,
                "item_index": source.item_index,
                "execution": {
                    **existing["result"],
                    "execution_status": "DUPLICATE",
                },
                "llm_called": False,
                "idempotent_replay": True,
            }

        if source.item["item_type"] == "NON_TASK_ITEM":
            command = self.validator.skip(source)
            result = self.executor.execute(command)
            return self._record(source, [], None, {}, command.as_dict(), result)

        candidates = self.retriever.retrieve(source)
        try:
            decision, model_audit = self.judge.judge(source, candidates)
            command = self.validator.build(source, decision, candidates)
        except Exception as error:
            model_audit = {
                "model_failure": type(error).__name__,
                "message": str(error)[:500],
            }
            command = self.validator.review_for_failure(
                source,
                candidates,
                f"LIFECYCLE_JUDGE_FAILURE: {error}",
            )
            decision = None
        try:
            result = self.executor.execute(command)
        except ExecutionConflict as error:
            review_command = self.validator.review_for_failure(
                source,
                candidates,
                f"EXECUTION_CONFLICT: {error}",
            )
            result = self.executor.execute(review_command)
            command = review_command
        return self._record(
            source,
            candidates,
            decision.as_dict() if decision else None,
            model_audit,
            command.as_dict(),
            result,
        )

    def process_file(
        self,
        m2_path: Path | str,
        *,
        output_path: Path | str,
        document_id: str | None = None,
        accept_review_input: bool = False,
    ) -> dict[str, Any]:
        resolved_document_id, contexts = load_m2_payload(
            m2_path,
            document_id=document_id,
            require_pass=True,
            accept_review=accept_review_input,
        )
        records = [self.process_context(context) for context in contexts]
        action_counts = Counter(
            record["execution"].get("action") for record in records
        )
        status_counts = Counter(
            record["execution"].get("execution_status") for record in records
        )
        output = {
            "schema_version": "m6.run_result.v1",
            "source_document_id": resolved_document_id,
            "input_path": str(Path(m2_path)),
            "input_validation_status": (
                contexts[0].m2_validation.get("status") if contexts else "PASS"
            ),
            "review_input_explicitly_accepted": bool(
                accept_review_input
                and contexts
                and contexts[0].m2_validation.get("status") == "REVIEW"
            ),
            "item_count": len(contexts),
            "action_counts": dict(action_counts),
            "execution_status_counts": dict(status_counts),
            "records": records,
        }
        atomic_write_json(output_path, output)
        return output

    @staticmethod
    def _record(
        source: SourceContext,
        candidates: list[dict[str, Any]],
        decision: dict[str, Any] | None,
        model_audit: dict[str, Any],
        command: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "source_document_id": source.source_document_id,
            "source_item_id": source.source_item_id,
            "item_index": source.item_index,
            "item_type": source.item["item_type"],
            "candidate_task_ids": [
                candidate["task_id"] for candidate in candidates
            ],
            "decision": decision,
            "model_audit": model_audit,
            "command": command,
            "execution": result,
            "llm_called": decision is not None or bool(model_audit),
            "idempotent_replay": False,
        }
