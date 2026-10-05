from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from src.lifecycle_judge import LifecycleJudge


ROOT = Path(__file__).resolve().parents[1]


def test_model_cannot_generate_ids_versions_or_arbitrary_values() -> None:
    schema=json.loads((ROOT/'schemas/lifecycle_decision.schema.json').read_text())
    validator=Draft202012Validator(schema)
    value={'decision':'CREATE','target_index':None,'fields':[],'scope':'same_task','reason':'新目标','evidence':None}
    assert not list(validator.iter_errors(value))
    for field in ('task_id','version','department_route','sql','changes'):
        assert list(validator.iter_errors({**value,field:'unauthorized'}))


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
    assert normalized == {'decision':'CREATE','target_task_id':None,'reason':'新任务','department_change':{}}
    # Only unwrap; old unsupported fields still fail the new model-output schema.
    schema=json.loads((ROOT/'schemas/lifecycle_decision.schema.json').read_text())
    assert list(Draft202012Validator(schema).iter_errors(normalized))
