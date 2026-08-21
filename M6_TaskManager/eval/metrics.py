"""Risk-oriented lifecycle evaluation metrics."""

from __future__ import annotations

from collections import Counter
from typing import Any


ACTIONS = (
    "CREATE",
    "PROGRESS_UPDATE",
    "MODIFY",
    "COMPLETE",
    "CANCEL",
    "REOPEN",
    "TRANSFER",
    "REVIEW",
)
TARGET_ACTIONS = set(ACTIONS) - {"CREATE", "REVIEW"}


def evaluate_predictions(
    samples: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(samples) != len(predictions):
        raise ValueError("Gold 与预测数量不一致")
    confusion: Counter[tuple[str, str]] = Counter()
    error_cases = {
        "false_updates": [],
        "false_creates": [],
        "wrong_targets": [],
        "reviews": [],
    }
    correct_decisions = 0
    target_total = 0
    target_correct = 0
    wrong_target = 0
    false_update = 0
    false_create = 0
    predicted_reviews = 0
    routing_total = 0
    routing_correct = 0

    for sample, prediction in zip(samples, predictions):
        gold = sample["gold"]
        gold_action = gold["decision"]
        predicted_action = prediction["decision"]
        confusion[(gold_action, predicted_action)] += 1
        if gold_action == predicted_action:
            correct_decisions += 1
        if gold_action in TARGET_ACTIONS:
            target_total += 1
            if prediction.get("target_task_id") == gold.get("target_task_id"):
                target_correct += 1
            elif predicted_action in TARGET_ACTIONS:
                wrong_target += 1
                error_cases["wrong_targets"].append(sample["sample_id"])
        if predicted_action in TARGET_ACTIONS and (
            gold_action not in TARGET_ACTIONS
            or prediction.get("target_task_id") != gold.get("target_task_id")
        ):
            false_update += 1
            error_cases["false_updates"].append(sample["sample_id"])
        if predicted_action == "CREATE" and gold_action != "CREATE":
            false_create += 1
            error_cases["false_creates"].append(sample["sample_id"])
        if predicted_action == "REVIEW":
            predicted_reviews += 1
            error_cases["reviews"].append(sample["sample_id"])
        expected_department = gold.get("to_department")
        if expected_department:
            routing_total += 1
            actual_change = prediction.get("department_change") or {}
            if actual_change.get("to_department") == expected_department:
                routing_correct += 1

    per_action = {}
    for action in ACTIONS:
        true_positive = confusion[(action, action)]
        predicted = sum(
            count
            for (gold_action, predicted_action), count in confusion.items()
            if predicted_action == action
        )
        actual = sum(
            count
            for (gold_action, _), count in confusion.items()
            if gold_action == action
        )
        per_action[action] = {
            "support": actual,
            "precision": true_positive / predicted if predicted else None,
            "recall": true_positive / actual if actual else None,
        }

    total = len(samples)
    return {
        "sample_count": total,
        "decision_accuracy": correct_decisions / total if total else 0.0,
        "per_action": per_action,
        "target_task_id_accuracy": (
            target_correct / target_total if target_total else None
        ),
        "wrong_target_rate": wrong_target / target_total if target_total else 0.0,
        "false_update_rate": false_update / total if total else 0.0,
        "false_create_rate": false_create / total if total else 0.0,
        "review_rate": predicted_reviews / total if total else 0.0,
        "department_routing_accuracy": (
            routing_correct / routing_total if routing_total else None
        ),
        "confusion": {
            f"{gold}->{predicted}": count
            for (gold, predicted), count in sorted(confusion.items())
        },
        "error_cases": error_cases,
    }
