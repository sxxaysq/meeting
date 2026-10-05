"""M6 任务业务目标粒度的确定性归并。"""

from __future__ import annotations

import copy
import re
from typing import Any


LEADING_MARKER = re.compile(
    r"^\s*(?:(?:[（(]?\d+[）)]|[①-⑳]|[一二三四五六七八九十]+[、.．])\s*)?"
)


def _business_anchor(text: str) -> str | None:
    """提取“项目/平台：工作环节”结构中的业务目标锚点。"""
    cleaned = LEADING_MARKER.sub("", text.strip(), count=1)
    parts = re.split(r"[:：]", cleaned, maxsplit=1)
    if len(parts) != 2:
        return None
    anchor = parts[0].strip(" \t\r\n：:")
    if not 2 <= len(anchor) <= 40:
        return None
    return anchor


def _task_title(anchor: str) -> str:
    if anchor.endswith("平台"):
        return f"构建{anchor}"
    if anchor.endswith(("系统", "中心")):
        return f"建设{anchor}"
    return f"推进{anchor}"


def _clean_item(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().rstrip("。；;")
    return cleaned or None


def _unique(values: list[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_item(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def normalize_task_granularity(
    batch: dict,
    source: dict,
) -> tuple[dict, dict[str, Any]]:
    """将同一业务锚点下的多个 CREATE 环节归并为一个主任务。

    只有当一个批次全部为 CREATE、责任主体一致，且每个标题都以来源
    冒号前的业务锚点开头时才归并。不同项目、不同责任主体或没有明确
    锚点的独立任务不会被合并。
    """
    commands = batch.get("commands", [])
    audit: dict[str, Any] = {
        "applied": False,
        "rule": "shared_business_goal",
        "before_count": len(commands),
        "after_count": len(commands),
    }
    if not commands or any(
        command.get("db_action") != "CREATE" for command in commands
    ):
        return batch, audit

    anchor = _business_anchor(source.get("text", ""))
    if not anchor:
        return batch, audit

    patches = [command.get("task_patch", {}) for command in commands]
    assignees = {
        patch.get("assignee_raw")
        for patch in patches
        if patch.get("assignee_raw") is not None
    }
    if len(assignees) > 1:
        return batch, audit

    titles = [str(patch.get("title") or "").strip() for patch in patches]
    if not titles or any(
        not LEADING_MARKER.sub("", title, count=1).startswith(anchor)
        for title in titles
    ):
        return batch, audit

    work_items: list[str | None] = []
    for patch in patches:
        patch_items = patch.get("work_items") or []
        if patch_items:
            work_items.extend(patch_items)
        else:
            work_items.append(patch.get("description"))
    work_items_clean = _unique(work_items)
    if len(work_items_clean) < 2:
        return batch, audit

    statuses = {patch.get("status") for patch in patches}
    deadlines = {
        patch.get("deadline_raw")
        for patch in patches
        if patch.get("deadline_raw") is not None
    }
    merged = copy.deepcopy(commands[0])
    merged["command_index"] = 1
    merged["task_patch"] = {
        "title": _task_title(anchor),
        "description": source["text"].strip(),
        "work_items": work_items_clean,
        "assignee_raw": next(iter(assignees), source.get("actor_hint")),
        "deadline_raw": next(iter(deadlines)) if len(deadlines) == 1 else None,
        "status": "in_progress" if "in_progress" in statuses else "open",
    }
    merged["evidence"] = {
        "segment_id": source["segment_id"],
        "subsegment_id": source["subsegment_id"],
        "text": source["text"],
    }
    merged["reason_code"] = (
        "MERGED_SHARED_BUSINESS_GOAL"
        if len(commands) > 1
        else "CANONICALIZED_SHARED_BUSINESS_GOAL"
    )
    merged["ambiguities"] = []

    normalized = copy.deepcopy(batch)
    normalized["commands"] = [merged]
    audit.update(
        {
            "applied": True,
            "anchor": anchor,
            "before_count": len(commands),
            "after_count": 1,
            "merged_titles": titles,
            "result_title": merged["task_patch"]["title"],
        }
    )
    return normalized, audit
