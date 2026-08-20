"""Evaluate old matcher, oracle plumbing, or the live lifecycle judge."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.metrics import evaluate_predictions
from eval.old_matcher_baseline import predict as old_predict
from src.candidate_retriever import CandidateRetriever
from src.command_validator import CommandValidator
from src.department_router import DepartmentRouter
from src.executor import TaskExecutor
from src.lifecycle_judge import LifecycleJudge
from src.llm_client import OpenAICompatibleLifecycleClient
from src.models import SourceContext
from src.repository import TaskRepository
from src.service import TaskLifecycleService


def load_samples(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = payload.get("samples")
    if not isinstance(samples, list):
        raise ValueError("Gold 文件缺少 samples 数组")
    defaults = payload.get("defaults") or {}
    item_defaults = defaults.get("current_item") or {}
    candidate_defaults = defaults.get("historical_candidate") or {}
    materialized = []
    for raw_sample in samples:
        sample = dict(raw_sample)
        current = {**item_defaults, **sample["current_item"]}
        if "evidence" not in current:
            content = current["content"]
            current["evidence"] = {
                "text": content,
                "page_start": None,
                "page_end": None,
                "start_char": 0,
                "end_char": len(content),
                "exact_match": True,
            }
        sample["current_item"] = current
        sample["historical_candidates"] = [
            {**candidate_defaults, **candidate}
            for candidate in sample.get("historical_candidates", [])
        ]
        materialized.append(sample)
    return materialized


def source_context(sample: dict[str, Any]) -> SourceContext:
    current = sample["current_item"]
    project_entity_id = sample.get("project_entity_id")
    return SourceContext(
        source_document_id=sample["source_document_id"],
        source_item_id=sample["source_item_id"],
        source_mode=sample.get("source_mode", "block"),
        item_index=0,
        item=current,
        merge_trace={
            "item_index": 0,
            "source_indexes": [0],
            "merged": False,
            "evidence_mode": "single",
            "evidence_contiguous": True,
            "source_evidence": [current["evidence"]],
            "project_entity_id": project_entity_id,
        },
        project_entity_id=project_entity_id,
        project_entities=[],
        m2_validation={"status": "PASS", "issues": []},
    )


def evaluate(
    samples: list[dict[str, Any]],
    *,
    mode: str,
    judge: LifecycleJudge | None = None,
) -> dict[str, Any]:
    predictions = []
    audits = []
    idempotency_violations = 0
    transaction_failures = 0
    for sample in samples:
        if mode == "old":
            prediction = old_predict(sample)
            audit = {"model": "old_dice_matcher"}
        elif mode == "oracle":
            gold = sample["gold"]
            prediction = {
                "decision": gold["decision"],
                "target_task_id": gold.get("target_task_id"),
                "reason": "oracle",
                "event_summary": None,
                "changes": {},
                "department_change": (
                    {
                        "from_department": gold.get("from_department"),
                        "to_department": gold["to_department"],
                        "evidence": gold.get("transfer_evidence", "oracle"),
                    }
                    if gold.get("to_department")
                    else None
                ),
            }
            audit = {"model": "oracle"}
        elif mode == "llm":
            if judge is None:
                raise ValueError("llm mode requires a LifecycleJudge")
            decision, audit = judge.judge(
                source_context(sample), sample["historical_candidates"]
            )
            prediction = decision.as_dict()
        else:
            if judge is None:
                raise ValueError("pipeline mode requires a LifecycleJudge")
            try:
                prediction, audit, idempotent = pipeline_predict(sample, judge)
                if not idempotent:
                    idempotency_violations += 1
            except Exception as error:
                transaction_failures += 1
                prediction = {
                    "decision": "REVIEW",
                    "target_task_id": None,
                    "reason": f"pipeline failure: {error}",
                    "event_summary": None,
                    "changes": {},
                    "department_change": None,
                }
                audit = {
                    "pipeline_failure": type(error).__name__,
                    "message": str(error)[:500],
                }
        predictions.append(prediction)
        audits.append({"sample_id": sample["sample_id"], **audit})
    metrics = evaluate_predictions(samples, predictions)
    metrics["idempotency_violations"] = idempotency_violations
    metrics["transaction_failures"] = transaction_failures
    return {
        "mode": mode,
        "metrics": metrics,
        "predictions": [
            {
                "sample_id": sample["sample_id"],
                "gold": sample["gold"],
                "prediction": prediction,
            }
            for sample, prediction in zip(samples, predictions)
        ],
        "audits": audits,
    }


def pipeline_predict(
    sample: dict[str, Any],
    judge: LifecycleJudge,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    with tempfile.TemporaryDirectory() as temporary:
        repository = TaskRepository(Path(temporary) / "m6.db")
        repository.initialize()
        repository.upsert_department(
            "D-IM",
            "智能矿山事业部",
            "department://智能矿山事业部/tasks",
        )
        repository.upsert_department(
            "D-RD", "研发中心", "department://研发中心/tasks"
        )
        repository.upsert_department(
            "D-MARKET",
            "市场经营部",
            "department://市场经营部/tasks",
        )
        department_ids = {
            "智能矿山事业部": "D-IM",
            "研发中心": "D-RD",
            "市场经营部": "D-MARKET",
        }
        for candidate in sample["historical_candidates"]:
            trusted = dict(candidate)
            trusted["department_id"] = department_ids[trusted["department"]]
            repository.add_historical_task(trusted)
        router = DepartmentRouter(repository)
        service = TaskLifecycleService(
            repository=repository,
            retriever=CandidateRetriever(repository, top_k=5, closed_quota=2),
            judge=judge,
            validator=CommandValidator(repository, router),
            executor=TaskExecutor(repository),
        )
        source = source_context(sample)
        first = service.process_context(source)
        second = service.process_context(source)
        command = first["command"]
        prediction = {
            "decision": command["action"],
            "target_task_id": command["target_task_id"],
            "reason": command["reason"],
            "event_summary": None,
            "changes": command["changes"],
            "department_change": command["department_change"],
        }
        idempotent = (
            second["execution"]["execution_status"] == "DUPLICATE"
            and second["llm_called"] is False
        )
        audit = {
            **first["model_audit"],
            "execution_status": first["execution"]["execution_status"],
            "idempotent_replay": idempotent,
        }
        return prediction, audit, idempotent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gold",
        type=Path,
        default=ROOT / "gold" / "lifecycle_gold_v1.json",
    )
    parser.add_argument(
        "--mode", choices=("old", "llm", "pipeline", "oracle"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--base-url",
        default=os.getenv("LLM_BASE_URL", "http://192.168.30.215:8000/v1"),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LLM_MODEL", "Qwen/Qwen3.6-35B-A3B"),
    )
    parser.add_argument("--api-key", default=os.getenv("LLM_API_KEY", "EMPTY"))
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    judge = None
    if args.mode in {"llm", "pipeline"}:
        judge = LifecycleJudge(
            OpenAICompatibleLifecycleClient(
                base_url=args.base_url,
                model=args.model,
                api_key=args.api_key,
                timeout_seconds=args.timeout,
            )
        )
    result = evaluate(load_samples(args.gold), mode=args.mode, judge=judge)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["metrics"], ensure_ascii=False))


if __name__ == "__main__":
    main()
