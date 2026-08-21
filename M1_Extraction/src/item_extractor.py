# -*- coding: utf-8 -*-
"""Block 级 Item 抽取：渲染提示词 → 调模型 → 结构归一 → Evidence 对齐。

这里只做**结构**归一（类型、形状、字段集合），不做语义修补。
模型给出非法 item_type 之类的问题时，该 Item 会被记入 dropped 并附原因，
不会被悄悄改成一个看起来合理的值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from evidence_aligner import build_evidence
from llm_client import LLMClient
from schema_validator import ITEM_TYPES
from structure_segmenter import Block
from text_normalizer import NormalizedDocument

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "item_extractor.md"
MAX_ASSIGNEE_CHARS = 8


@dataclass
class BlockResult:
    block_index: int
    items: List[dict] = field(default_factory=list)
    dropped: List[dict] = field(default_factory=list)
    warnings: List[dict] = field(default_factory=list)
    error: Optional[str] = None


def load_prompt(path: Optional[Path] = None) -> str:
    return (path or PROMPT_PATH).read_text(encoding="utf-8")


def render_prompt(template: str, block: Block) -> Tuple[str, str]:
    """提示词里 Block 上下文与正文都在 user 侧，system 只放规则，便于复用缓存。"""
    marker = "## Block 上下文"
    head, _, tail = template.partition(marker)
    body = (marker + tail)
    body = (
        body.replace("{{department}}", block.department or "（未标注）")
        .replace("{{work_section}}", block.work_section or "（未标注）")
        .replace("{{delivery_group}}", block.delivery_group or "（未标注）")
        .replace("{{raw_text}}", block.raw_text)
    )
    return head.strip(), body.strip()


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _check_project(project: str, block: Block, doc: NormalizedDocument):
    """project 必须是原文里出现过的连续文字。

    返回 (采纳的值, 告警)。模型最常见的越界是**拼接**：
    把上级单位名和项目名拼成"天地奔牛ERP项目"、"太原院MOM 二期"，
    或给没有"项目"二字的名称硬加后缀。这类值在原文中根本不存在，
    属于 M1 明令禁止的项目归一，只能置空——
    "没有明确项目归属就填 null"比编一个名字忠实。
    """
    compact = _compact(project)
    if compact in _compact(block.raw_text):
        return project, None
    if compact in _compact(doc.text):
        # 在文档别处出现：可能是合法的上级上下文继承，保留但留痕
        return project, "project {!r} 不在本 Block 内，来自文档其他位置".format(project)
    return None, "project {!r} 非原文连续片段，已置空（M1 禁止拼接/归一项目名）".format(
        project
    )


def _clean_optional(value) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _clean_assignee(value) -> Optional[List[str]]:
    """assignee 必须是字符串数组；模型给 null / 字符串时做保守修形。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in value.replace("、", "/").split("/")]
    if not isinstance(value, list):
        return None
    names: List[str] = []
    for entry in value:
        if not isinstance(entry, str):
            return None
        name = entry.strip()
        if not name:
            continue
        if len(name) > MAX_ASSIGNEE_CHARS:
            # 过长基本是把单位名当成了人名，交给质量门报错而不是硬塞
            return None
        if name not in names:
            names.append(name)
    return names


def normalize_item(
    raw: dict, block: Block, doc: NormalizedDocument
) -> Tuple[Optional[dict], Optional[str], List[str]]:
    """返回 (item, 丢弃原因, 告警列表)。

    只有整条 Item 不可用时才丢弃。assignee 这类**单个字段**形状不对时，
    清空该字段并记一条告警——为了一个附属字段丢掉整条业务事项，
    对以高召回为目标的 M1 是更糟的选择。告警会进 report，不是静默修正。
    """
    warnings: List[str] = []
    if not isinstance(raw, dict):
        return None, "不是对象", warnings

    item_type = raw.get("item_type")
    if item_type not in ITEM_TYPES:
        return None, "非法 item_type: {!r}".format(item_type), warnings

    title = _clean_optional(raw.get("title"))
    content = _clean_optional(raw.get("content"))
    if not title:
        return None, "title 为空", warnings
    if not content:
        return None, "content 为空", warnings

    assignee = _clean_assignee(raw.get("assignee"))
    if assignee is None:
        warnings.append("assignee 形状非法已清空: {!r}".format(raw.get("assignee")))
        assignee = []

    evidence_text = _clean_optional(raw.get("evidence_text")) or _clean_optional(
        (raw.get("evidence") or {}).get("text") if isinstance(raw.get("evidence"), dict) else None
    )
    if not evidence_text:
        # 没有证据文本时退回 content，让对齐器如实判断能否定位
        evidence_text = content

    project = _clean_optional(raw.get("project"))
    if project:
        project, project_warning = _check_project(project, block, doc)
        if project_warning:
            warnings.append(project_warning)

    item = {
        "department": _clean_optional(raw.get("department")) or block.department,
        "work_section": _clean_optional(raw.get("work_section")) or block.work_section,
        "delivery_group": _clean_optional(raw.get("delivery_group")) or block.delivery_group,
        "project": project,
        "item_type": item_type,
        "assignee": assignee,
        "title": title,
        "content": content,
        "evidence": build_evidence(doc, block, evidence_text),
    }
    return item, None, warnings


def extract_block(
    client: LLMClient, block: Block, doc: NormalizedDocument, template: str
) -> BlockResult:
    result = BlockResult(block_index=block.index)
    system, user = render_prompt(template, block)
    payload = client.complete_json(system, user)
    if payload is None:
        result.error = "模型未返回合法 JSON（已重试一次）"
        return result

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        result.error = "模型输出缺少 items 数组"
        return result

    for position, raw in enumerate(raw_items):
        item, reason, warnings = normalize_item(raw, block, doc)
        for warning in warnings:
            result.warnings.append(
                {"block_index": block.index, "position": position, "detail": warning}
            )
        if item is None:
            result.dropped.append(
                {
                    "block_index": block.index,
                    "position": position,
                    "reason": reason,
                    "raw": raw,
                }
            )
            continue
        result.items.append(item)
    return result
