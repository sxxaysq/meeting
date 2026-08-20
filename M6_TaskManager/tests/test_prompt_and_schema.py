from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from src.lifecycle_judge import LifecycleJudge


ROOT = Path(__file__).resolve().parents[1]


def test_prompt_contains_hard_identity_boundaries() -> None:
    prompt = (ROOT / "prompts" / "lifecycle_judge.md").read_text(
        encoding="utf-8"
    )
    for statement in (
        "同项目不代表同任务",
        "同负责人也不代表同任务",
        "子步骤不代表整个任务 COMPLETE",
        "必须 REVIEW",
        "不能返回候选列表之外",
        "既陈述某个历史任务整体完成",
        "禁止使用",
    ):
        assert statement in prompt


def test_published_schemas_are_valid() -> None:
    for name in ("lifecycle_decision.schema.json", "task_command.schema.json"):
        schema = json.loads(
            (ROOT / "schemas" / name).read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)


def test_judge_normalizes_only_unambiguous_json_wrappers() -> None:
    normalized = LifecycleJudge._normalize_shape(
        {
            "lifecycle_decision": {
                "decision": "CREATE",
                "target_task_id": None,
                "reason": "新任务",
                "department_change": {},
            }
        }
    )
    assert normalized == {
        "decision": "CREATE",
        "target_task_id": None,
        "reason": "新任务",
        "event_summary": None,
        "changes": {},
        "department_change": None,
    }
