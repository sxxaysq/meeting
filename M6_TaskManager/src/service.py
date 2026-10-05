"""Validated M2 items to per-item, per-operation lifecycle execution."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .candidate_retriever import CandidateRetriever, same_goal
from .command_validator import CommandValidator
from .executor import TaskExecutor
from .lifecycle_judge import LifecycleJudge
from .m2_input import load_m2_payload
from .models import ExecutionConflict, SourceContext, TechnicalFailure, DecisionValidationError
from .repository import TaskRepository
from .input_policy import restriction, pending_restriction


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

        blocked = restriction(source)

        if source.item["item_type"] == "NON_TASK_ITEM":
            command = self.validator.skip(source)
            result = self.executor.execute(command)
            return self._record(source, [], None, {}, command.as_dict(), result)

        if blocked:
            command = self.validator.review_for_failure(source, [], blocked)
            result = self.executor.execute(command)
            return self._record(source, [], None, {}, command.as_dict(), result)

        candidates=self.retriever.retrieve(source)
        expanded=transport_retried=version_retried=duplicate_rechecked=False
        recovery=[]; correction=None; audits=[]
        before={k:getattr(self.judge,k,0) for k in ('calls','format_retries','service_failures')}
        for _ in range(4):  # one expansion, one version refresh, one transport retry; bounded overall
            try:
                decision,audit=self.judge.judge(source,candidates,expanded=expanded,correction=correction)
                audits.append(audit)
                if not expanded and (decision.expand or decision.decision.value in ('CREATE','REVIEW')):
                    wider=self.retriever.retrieve(source,expanded=True)
                    expanded=True
                    if decision.expand or {c['task_id'] for c in wider}!={c['task_id'] for c in candidates}:
                        candidates=wider;recovery.append('expanded_retrieval');continue
                if decision.expand:
                    command=self.validator.review_for_failure(source,candidates,'BUSINESS_REVIEW: 扩展检索后仍无法确定具体业务目标')
                else:
                    if decision.decision.value=='CREATE' and not duplicate_rechecked and any(same_goal(source.item,c) for c in candidates):
                        duplicate_rechecked=True;correction='候选中已有同名或同内容的具体目标，请判断进展、重复事实或真实歧义，不重复新建。'
                        recovery.append('duplicate_recheck');continue
                    command=self.validator.build(source,decision,candidates)
                    if command.action != decision.decision and command.action.value != 'REVIEW':
                        recovery.append('source_grounded_action_fallback')
                result=self.executor.execute(command)
                metrics={k:getattr(self.judge,k,0)-before[k] for k in before}
                return self._record(source,candidates,decision.as_dict(),{'attempts':audits,'recovery':recovery,**metrics},command.as_dict(),result)
            except TechnicalFailure as error:
                if not transport_retried and str(error).startswith('MODEL_SERVICE'):
                    transport_retried=True;recovery.append('model_service_retry');continue
                return self._failure(source,'TECHNICAL_FAILURE',str(error),recovery,before)
            except ExecutionConflict as error:
                if not version_retried:
                    version_retried=True;candidates=self.retriever.retrieve(source,expanded=expanded)
                    correction='目标版本发生变化，已重新读取，请根据最新状态重新判断。'
                    recovery.append('version_refresh');continue
                return self._failure(source,'TECHNICAL_FAILURE','VERSION_CONFLICT: '+str(error),recovery,before)
            except DecisionValidationError as error:
                return self._failure(source,'REJECTED',str(error),recovery,before)
        return self._failure(source,'TECHNICAL_FAILURE','RECOVERY_BUDGET_EXHAUSTED',recovery,before)

    def _failure(self,source,kind,reason,recovery,before):
        # Technical failures remain retryable: no terminal processing_record and no review row.
        from .repository import utc_now
        now=utc_now();audit_id='FAIL-'+uuid.uuid4().hex
        result={'action':kind,'execution_status':kind,'task_id':None,'event_id':None,'audit_id':audit_id}
        with self.repository.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT result_json FROM processing_records WHERE source_document_id=? AND source_item_id=?',
                               (source.source_document_id,source.source_item_id)).fetchone()
            if existing:
                result={**json.loads(existing['result_json']),'execution_status':'DUPLICATE'}
            else:
                c.execute('INSERT INTO task_audit(audit_id,action,reason,source_document_id,source_item_id,provenance_json,created_at) VALUES (?,?,?,?,?,?,?)',
                    (audit_id,kind,reason,source.source_document_id,source.source_item_id,json.dumps({**source.provenance,'item':source.item},ensure_ascii=False),now))
                if kind=='REJECTED':
                    c.execute('INSERT INTO processing_records(processing_id,source_document_id,source_item_id,action,result_json,created_at) VALUES (?,?,?,?,?,?)',
                        ('PR-'+uuid.uuid4().hex,source.source_document_id,source.source_item_id,kind,json.dumps(result),now))
            c.commit()
        metrics={k:getattr(self.judge,k,0)-before[k] for k in before}
        return {'source_document_id':source.source_document_id,'source_item_id':source.source_item_id,
                'item_index':source.item_index,'failure_kind':kind,'reason':reason,'execution':result,
                'model_audit':{'recovery':recovery,**metrics},'llm_called':metrics['calls']>0,'idempotent_replay':False}

    def process_file(
        self,
        m2_path: Path | str,
        *,
        output_path: Path | str,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_document_id, contexts = load_m2_payload(
            m2_path,
            document_id=document_id,
            require_pass=False,
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
