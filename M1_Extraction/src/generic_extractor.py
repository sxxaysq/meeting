# -*- coding: utf-8 -*-
"""Whole-document generic fallback extractor.

This path is deliberately high-recall and format-tolerant. It feeds the whole
normalized document to a generic meeting-task prompt and maps the returned
``tasks`` array into the existing M1 ``items`` schema. It does not split or
merge semantically similar items; downstream M2 can handle consolidation and
stricter quality decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from evidence_aligner import build_evidence
from llm_client import LLMClient
from structure_segmenter import Block
from text_normalizer import NormalizedDocument

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "generic_item_extractor.md"
SYSTEM_MARKER = "## SYSTEM_PROMPT"
USER_MARKER = "## USER_PROMPT_TEMPLATE"
MAX_ASSIGNEE_CHARS = 8

TASK_CATEGORY_TO_ITEM_TYPE = {
    "PROJECT_TASK": "PROJECT_TASK",
    "RESEARCH_TASK": "RESEARCH_TASK",
    "GENERAL_WORK": "NON_PROJECT_WORK",
    "NON_TASK_ITEM": "NON_TASK_ITEM",
}


@dataclass
class GenericResult:
    items: List[dict] = field(default_factory=list)
    dropped: List[dict] = field(default_factory=list)
    warnings: List[dict] = field(default_factory=list)
    error: Optional[str] = None
    raw_task_count: int = 0


def load_prompt(path: Optional[Path] = None) -> str:
    return (path or PROMPT_PATH).read_text(encoding="utf-8")


def render_prompt(template: str, doc: NormalizedDocument) -> Tuple[str, str]:
    """Split the stored prompt into system/user messages and inject document text."""
    if SYSTEM_MARKER not in template or USER_MARKER not in template:
        raise RuntimeError("generic prompt must contain SYSTEM_PROMPT and USER_PROMPT_TEMPLATE")
    _, _, rest = template.partition(SYSTEM_MARKER)
    system, _, user_template = rest.partition(USER_MARKER)
    user = user_template.replace("{{meeting_text}}", doc.text)
    return system.strip(), user.strip()


def whole_document_block(doc: NormalizedDocument) -> Block:
    return Block(
        index=0,
        department=None,
        work_section=None,
        delivery_group=None,
        raw_text=doc.text,
        start_char=0,
        end_char=len(doc.text),
        page_start=doc.page_of(0) if doc.text else None,
        page_end=doc.page_of(max(0, len(doc.text) - 1)) if doc.text else None,
    )


def _clean_optional(value) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _clean_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _clean_assignee(value) -> Tuple[Optional[List[str]], Optional[str]]:
    if value is None:
        return [], None
    if isinstance(value, str):
        value = [part for part in value.replace("、", "/").split("/")]
    if not isinstance(value, list):
        return [], "assignee 形状非法已清空: {!r}".format(value)
    names: List[str] = []
    for entry in value:
        if not isinstance(entry, str):
            return [], "assignee 含非字符串值，已清空: {!r}".format(value)
        name = entry.strip()
        if not name:
            continue
        if len(name) > MAX_ASSIGNEE_CHARS:
            return [], "assignee 过长，疑似单位名，已清空: {!r}".format(value)
        if name not in names:
            names.append(name)
    return names, None


def normalize_task(raw: dict, doc: NormalizedDocument, block: Block, position: int):
    warnings: List[str] = []
    if not isinstance(raw, dict):
        return None, "不是对象", warnings

    category = _clean_text(raw.get("task_category"))
    item_type = TASK_CATEGORY_TO_ITEM_TYPE.get(category)
    if item_type is None:
        return None, "非法 task_category: {!r}".format(raw.get("task_category")), warnings

    title = _clean_text(raw.get("title"))
    content = _clean_text(raw.get("description"))
    if not title:
        return None, "title 为空", warnings
    if not content:
        return None, "description 为空", warnings

    assignee, warning = _clean_assignee(raw.get("assignee"))
    if warning:
        warnings.append(warning)

    evidence_text = _clean_text(raw.get("source_segment")) or content or title
    item = {
        "department": _clean_optional(raw.get("department")),
        "work_section": None,
        "delivery_group": _clean_optional(raw.get("project_group")),
        "project": _clean_optional(raw.get("project")),
        "item_type": item_type,
        "assignee": assignee or [],
        "title": title,
        "content": content,
        "evidence": build_evidence(doc, block, evidence_text),
    }
    return item, None, warnings


def extract_document_generic(
    client: LLMClient,
    doc: NormalizedDocument,
    template: Optional[str] = None,
) -> GenericResult:
    result = GenericResult()
    prompt = template or load_prompt()
    system, user = render_prompt(prompt, doc)
    payload = client.complete_json(system, user)
    if payload is None:
        result.error = "模型未返回合法 JSON（已重试一次）"
        return result

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        result.error = "模型输出缺少 tasks 数组"
        return result
    result.raw_task_count = len(raw_tasks)

    block = whole_document_block(doc)
    for position, raw in enumerate(raw_tasks):
        item, reason, warnings = normalize_task(raw, doc, block, position)
        for warning in warnings:
            result.warnings.append({"position": position, "detail": warning})
        if item is None:
            result.dropped.append({"position": position, "reason": reason, "raw": raw})
            continue
        result.items.append(item)
    return result
