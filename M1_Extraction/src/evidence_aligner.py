# -*- coding: utf-8 -*-
"""Evidence 对齐：把 LLM 给出的 evidence_text 定位回规范化文本的字符区间。

分工（对应需求第十一节）：
  * LLM 只负责给出 evidence_text；
  * page_start / page_end / start_char / end_char / exact_match
    全部由本模块确定性计算，绝不让模型猜。

定位策略，逐级降级：
  1. 在 Block 范围内直接子串匹配；
  2. "归约匹配"——忽略空白与行首编号后匹配。原文 "①集控中心：…" 与
     模型回抄的 "集控中心：…" 视为同一段文本，这是**完整定位**，
     所以仍然算 exact_match=true；
  3. 锚点兜底——用首尾片段圈出最合理的原文范围，exact_match=false。

任何情况下都不会伪造 exact_match=true：只有当 evidence_text 的每一个
有效字符都在原文中连续出现时，才会返回 true。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from structure_segmenter import Block
from text_normalizer import ANY_ENUM, NormalizedDocument

ANCHOR_CHARS = 12
MIN_ANCHOR = 4


@dataclass
class Alignment:
    start_char: int
    end_char: int
    page_start: Optional[int]
    page_end: Optional[int]
    exact_match: bool
    strategy: str


def _reduce(text: str, base_offset: int) -> Tuple[str, List[int]]:
    """归约：逐行去掉行首编号、去掉所有空白，返回归约串与每个字符的绝对偏移。"""
    chars: List[str] = []
    offsets: List[int] = []
    cursor = 0
    for line in text.split("\n"):
        match = ANY_ENUM.match(line)
        start = match.end() if match else 0
        # 编号后可能跟着 "." "、" 空格
        while start < len(line) and line[start] in " 　.、．":
            start += 1
        for index in range(start, len(line)):
            char = line[index]
            if char.isspace():
                continue
            chars.append(char)
            offsets.append(base_offset + cursor + index)
        cursor += len(line) + 1
    return "".join(chars), offsets


def _significant(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _snap_to_line_edges(doc: NormalizedDocument, start: int, end: int) -> Tuple[int, int]:
    """把区间吸附到条目行边界。

    模型回抄时常常省掉行首的 ``①``，命中区间就会从编号之后开始。
    行首只剩编号、行尾只剩空白时向外吸附，取"最合理的原文范围"，
    与历史标注 start_char/end_char 的口径保持一致。
    """
    first = doc.line_at(start)
    if first is not None and start > first.start:
        prefix = doc.text[first.start : start]
        match = ANY_ENUM.match(prefix)
        if match and not prefix[match.end() :].strip(" 　.、．:："):
            start = first.start
    last = doc.line_at(max(start, end - 1))
    if last is not None and end < last.end and not doc.text[end : last.end].strip():
        end = last.end
    return start, end


def align_evidence(
    doc: NormalizedDocument, block: Block, evidence_text: str
) -> Alignment:
    region = doc.text[block.start_char : block.end_char]
    base = block.start_char

    def finish(start: int, end: int, exact: bool, strategy: str) -> Alignment:
        start, end = _snap_to_line_edges(doc, start, end)
        start = max(block.start_char, min(start, block.end_char))
        end = max(start, min(end, block.end_char))
        page_start, page_end = doc.page_range(start, end)
        return Alignment(start, end, page_start, page_end, exact, strategy)

    evidence_text = (evidence_text or "").strip()
    if not evidence_text:
        return finish(block.start_char, block.end_char, False, "empty")

    # 1) 直接子串
    index = region.find(evidence_text)
    if index >= 0:
        return finish(base + index, base + index + len(evidence_text), True, "exact")

    # 2) 归约匹配（忽略空白与行首编号）
    reduced_region, region_offsets = _reduce(region, base)
    reduced_evidence, _ = _reduce(evidence_text, 0)
    if reduced_evidence:
        index = reduced_region.find(reduced_evidence)
        if index >= 0:
            start = region_offsets[index]
            end = region_offsets[index + len(reduced_evidence) - 1] + 1
            return finish(start, end, True, "reduced")

    # 3) 锚点兜底：首尾片段各自定位，圈出最合理范围
    head = reduced_evidence[:ANCHOR_CHARS]
    tail = reduced_evidence[-ANCHOR_CHARS:]
    head_index = reduced_region.find(head) if len(head) >= MIN_ANCHOR else -1
    tail_index = reduced_region.rfind(tail) if len(tail) >= MIN_ANCHOR else -1
    if head_index >= 0 and tail_index >= 0 and tail_index >= head_index:
        start = region_offsets[head_index]
        end = region_offsets[min(tail_index + len(tail), len(region_offsets)) - 1] + 1
        return finish(start, end, False, "anchor_both")
    if head_index >= 0:
        start = region_offsets[head_index]
        line = doc.line_at(start)
        return finish(start, line.end if line else block.end_char, False, "anchor_head")
    if tail_index >= 0:
        end = region_offsets[min(tail_index + len(tail), len(region_offsets)) - 1] + 1
        line = doc.line_at(max(block.start_char, end - 1))
        return finish(line.start if line else block.start_char, end, False, "anchor_tail")

    # 4) 完全定位不到：保留整个 Block 范围，并如实标记 false
    return finish(block.start_char, block.end_char, False, "block_fallback")


def build_evidence(doc: NormalizedDocument, block: Block, evidence_text: str) -> dict:
    alignment = align_evidence(doc, block, evidence_text)
    return {
        "text": evidence_text,
        "page_start": alignment.page_start,
        "page_end": alignment.page_end,
        "start_char": alignment.start_char,
        "end_char": alignment.end_char,
        "exact_match": alignment.exact_match,
    }
