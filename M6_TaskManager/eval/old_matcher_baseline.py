"""Reproduce the old Dice-threshold matcher as an explicit baseline."""

from __future__ import annotations

from typing import Any

from src.candidate_retriever import dice, normalize_text


def predict(sample: dict[str, Any]) -> dict[str, Any]:
    current = sample["current_item"]
    candidates = [
        candidate
        for candidate in sample.get("historical_candidates", [])
        if candidate.get("status") not in {"COMPLETED", "CANCELLED", "CLOSED"}
    ]
    project = normalize_text(current.get("project"))
    if project:
        project_candidates = [
            candidate
            for candidate in candidates
            if normalize_text(candidate.get("project")) == project
        ]
        candidates = project_candidates or candidates
    ranked = []
    for candidate in candidates:
        title_score = dice(current.get("title"), candidate.get("title"))
        description_score = dice(
            current.get("content"), candidate.get("description")
        )
        score = 0.65 * title_score + 0.35 * description_score
        ranked.append((score, candidate))
    ranked.sort(key=lambda entry: (-entry[0], entry[1]["task_id"]))
    if not ranked:
        action = "CREATE"
        target = None
        reason = "old:no_active_candidate"
    else:
        top_score, top = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if top_score >= 0.55 and top_score - second_score >= 0.10:
            action = "PROGRESS_UPDATE"
            target = top["task_id"]
            reason = f"old:dice_update:{top_score:.3f}"
        elif top_score >= 0.42:
            action = "REVIEW"
            target = None
            reason = f"old:dice_ambiguous:{top_score:.3f}"
        else:
            action = "CREATE"
            target = None
            reason = f"old:dice_create:{top_score:.3f}"
    return {
        "decision": action,
        "target_task_id": target,
        "reason": reason,
        "event_summary": None,
        "changes": {},
        "department_change": None,
    }
