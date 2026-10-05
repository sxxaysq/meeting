from __future__ import annotations

from eval.metrics import evaluate_predictions
from eval.evaluate_lifecycle import load_samples


def sample(sample_id, decision, target=None):
    return {
        "sample_id": sample_id,
        "gold": {"decision": decision, "target_task_id": target},
    }


def prediction(decision, target=None):
    return {"decision": decision, "target_task_id": target}


def test_risk_metrics_distinguish_wrong_target_and_false_create() -> None:
    metrics = evaluate_predictions(
        [
            sample("s1", "PROGRESS_UPDATE", "T1"),
            sample("s2", "CREATE"),
            sample("s3", "COMPLETE", "T3"),
        ],
        [
            prediction("PROGRESS_UPDATE", "T2"),
            prediction("PROGRESS_UPDATE", "T9"),
            prediction("CREATE"),
        ],
    )
    assert metrics["wrong_target_rate"] == 0.5
    assert metrics["false_update_rate"] == 2 / 3
    assert metrics["false_create_rate"] == 1 / 3
    assert metrics["error_cases"]["wrong_targets"] == ["s1"]


def test_gold_defaults_materialize_full_item_contract() -> None:
    from pathlib import Path

    samples = load_samples(
        Path(__file__).resolve().parents[1]
        / "gold"
        / "lifecycle_gold_v1.json"
    )
    assert len(samples) == 28
    assert set(samples[0]["current_item"]) == {
        "department",
        "work_section",
        "delivery_group",
        "project",
        "item_type",
        "assignee",
        "title",
        "content",
        "evidence",
    }
