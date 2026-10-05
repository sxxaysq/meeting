"""从当前活动任务中确定可安全自动修改的唯一目标。"""

from __future__ import annotations

import re
from typing import Iterable


TASK_ID_PATTERN = re.compile(r"\bT\d{6,}\b", re.IGNORECASE)


def _compact(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"[\s，。；、：:（）()《》“”\"'_\-]+", "", text).lower()


def _bigrams(text: str) -> set[str]:
    value = _compact(text)
    if len(value) < 2:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}


def _dice(left: str, right: str) -> float:
    first = _bigrams(left)
    second = _bigrams(right)
    if not first or not second:
        return 0.0
    return 2.0 * len(first & second) / (len(first) + len(second))


def _task_view(task: dict, score: float, match_reasons: list[str]) -> dict:
    return {
        "task_id": task["task_id"],
        "title": task["title"],
        "description": task.get("description"),
        "assignee_raw": task.get("assignee_raw"),
        "deadline_raw": task.get("deadline_raw"),
        "status": task["status"],
        "version": task["version"],
        "match_score": round(score, 4),
        "match_reasons": match_reasons,
    }


class TargetMatcher:
    def __init__(
        self,
        max_candidates: int = 5,
        similarity_threshold: float = 0.48,
        uniqueness_gap: float = 0.12,
    ) -> None:
        self.max_candidates = max_candidates
        self.similarity_threshold = similarity_threshold
        self.uniqueness_gap = uniqueness_gap

    def match(self, text: str, tasks: Iterable[dict]) -> dict:
        task_list = list(tasks)
        explicit_ids = {match.upper() for match in TASK_ID_PATTERN.findall(text)}
        ranked: list[tuple[float, dict, list[str]]] = []
        compact_text = _compact(text)

        for task in task_list:
            reasons: list[str] = []
            task_id = str(task["task_id"]).upper()
            title = str(task.get("title") or "")
            assignee = str(task.get("assignee_raw") or "")
            if task_id in explicit_ids:
                score = 1.0
                reasons.append("explicit_task_id")
            elif _compact(title) and _compact(title) in compact_text:
                score = 0.95
                reasons.append("exact_title")
            else:
                title_score = _dice(text, title)
                description_score = _dice(text, str(task.get("description") or ""))
                score = max(title_score, description_score * 0.85)
                if score:
                    reasons.append("text_similarity")
                if assignee and _compact(assignee) in compact_text:
                    score = min(1.0, score + 0.08)
                    reasons.append("assignee")
            if score > 0:
                ranked.append((score, task, reasons))

        ranked.sort(key=lambda item: (-item[0], item[1]["task_id"]))
        selected = ranked[: self.max_candidates]
        candidates = [
            _task_view(task, score, reasons)
            for score, task, reasons in selected
        ]

        unique_target_task_id = None
        match_mode = "unmatched"
        if selected:
            top_score, top_task, top_reasons = selected[0]
            second_score = selected[1][0] if len(selected) > 1 else 0.0
            if "explicit_task_id" in top_reasons:
                unique_target_task_id = top_task["task_id"]
                match_mode = "explicit_task_id"
            elif "exact_title" in top_reasons and (
                len(selected) == 1 or second_score < 0.95
            ):
                unique_target_task_id = top_task["task_id"]
                match_mode = "exact_title"
            elif (
                top_score >= self.similarity_threshold
                and top_score - second_score >= self.uniqueness_gap
            ):
                unique_target_task_id = top_task["task_id"]
                match_mode = "unique_similarity"
            else:
                match_mode = "ambiguous"

        return {
            "candidates": candidates,
            "unique_target_task_id": unique_target_task_id,
            "match_mode": match_mode,
        }

