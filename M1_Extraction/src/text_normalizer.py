# -*- coding: utf-8 -*-
"""文本规范化：把 PDF 抽取出的物理行还原成"逻辑条目行"，并保留字符级偏移映射。

规范化步骤（顺序固定，任何改动都会让历史标注的 start_char/end_char 失效）：
  1. 删除 ``— 3 —`` 形式的页眉页脚页码行；
  2. 全角空格 U+3000 → 半角空格；
  3. 按编号标记把被 PDF 换行拆开的同一条目合并成一行；
  4. 逐行 strip，丢弃空行，用 ``\\n`` 连接。

规范化后的每一行 = 原文的一个"最内层编号条目"，
这是 M1 全流程的坐标系：blocks 和 evidence 的 start_char/end_char 都基于它。

``char_source[i]`` 保存规范化文本第 i 个字符在 raw_text 中的偏移，
用于把 evidence 位置反查回 PDF 页码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from document_reader import RawDocument

# 页码行：可能带前后换行，形如 "— 12 —"
PAGE_NUMBER = re.compile(r"\n?—\s*\d+\s*—\n?")

# 任意层级的编号前缀。顺序重要：先匹配长的（1.1），再匹配短的（1.）。
ANY_ENUM = re.compile(
    r"^(（[一二三四五六七八九十]+）"
    r"|[一二三四五六七八九十]+[、．]"
    r"|\d+\.\d+"
    r"|\d+[\.、．](?!\d)"
    r"|[（(]\d+[）)]"
    r"|\d+）"
    r"|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])"
)


@dataclass
class UnitLine:
    """规范化文本中的一行 = 原文一个编号条目。"""

    index: int
    text: str
    start: int  # 规范化文本坐标
    end: int


@dataclass
class NormalizedDocument:
    source_name: str
    text: str
    char_source: List[int] = field(default_factory=list)
    lines: List[UnitLine] = field(default_factory=list)
    raw: Optional[RawDocument] = None

    # ---- 位置换算 -------------------------------------------------
    def raw_offset(self, offset: int) -> Optional[int]:
        if not self.char_source:
            return None
        offset = max(0, min(offset, len(self.char_source) - 1))
        return self.char_source[offset]

    def page_of(self, offset: int) -> Optional[int]:
        if self.raw is None or not self.raw.page_spans:
            return None
        raw_offset = self.raw_offset(offset)
        if raw_offset is None:
            return None
        return self.raw.page_of(raw_offset)

    def page_range(self, start: int, end: int) -> Tuple[Optional[int], Optional[int]]:
        if self.raw is None or not self.raw.page_spans:
            return None, None
        return self.page_of(start), self.page_of(max(start, end - 1))

    def line_at(self, offset: int) -> Optional[UnitLine]:
        for line in self.lines:
            if line.start <= offset < line.end:
                return line
            if line.start > offset:
                break
        return None

    def expand_to_lines(self, start: int, end: int) -> Tuple[int, int]:
        """把任意字符区间外扩到完整条目行边界（历史标注采用的口径）。"""
        covered = [ln for ln in self.lines if ln.start < end and ln.end > start]
        if not covered:
            return start, end
        return covered[0].start, covered[-1].end


def _strip_page_numbers(text: str) -> Tuple[str, List[int]]:
    """删除页码行，返回新文本以及每个字符的原始偏移。"""
    out_chars: List[str] = []
    out_source: List[int] = []
    cursor = 0
    for match in PAGE_NUMBER.finditer(text):
        chunk = text[cursor : match.start()]
        out_chars.append(chunk)
        out_source.extend(range(cursor, match.start()))
        # 页码整体替换成一个换行，占位偏移指向匹配起点
        out_chars.append("\n")
        out_source.append(match.start())
        cursor = match.end()
    out_chars.append(text[cursor:])
    out_source.extend(range(cursor, len(text)))
    return "".join(out_chars), out_source


def normalize(raw: RawDocument) -> NormalizedDocument:
    stage1, stage1_source = _strip_page_numbers(raw.raw_text)
    # U+3000 → 空格 是 1:1 替换，偏移不变
    stage1 = stage1.replace("　", " ")

    # 按物理行切开，记录每个字符在 stage1 中的偏移
    units: List[List[Tuple[str, int]]] = []
    current: List[Tuple[str, int]] = []
    cursor = 0
    for physical in stage1.split("\n"):
        span = [(ch, stage1_source[cursor + i]) for i, ch in enumerate(physical)]
        cursor += len(physical) + 1  # +1 是被 split 吃掉的换行
        # strip：去掉首尾空白字符，同时丢弃对应偏移
        while span and span[0][0].isspace():
            span.pop(0)
        while span and span[-1][0].isspace():
            span.pop()
        if not span:
            continue
        line_text = "".join(ch for ch, _ in span)
        if ANY_ENUM.match(line_text):
            if current:
                units.append(current)
            current = span
        else:
            current.extend(span)
    if current:
        units.append(current)

    # 拼装规范化文本与偏移表
    text_parts: List[str] = []
    char_source: List[int] = []
    lines: List[UnitLine] = []
    position = 0
    for index, unit in enumerate(units):
        if index:
            text_parts.append("\n")
            char_source.append(unit[0][1])
            position += 1
        unit_text = "".join(ch for ch, _ in unit)
        text_parts.append(unit_text)
        char_source.extend(src for _, src in unit)
        lines.append(
            UnitLine(index=index, text=unit_text, start=position, end=position + len(unit_text))
        )
        position += len(unit_text)

    return NormalizedDocument(
        source_name=raw.source_name,
        text="".join(text_parts),
        char_source=char_source,
        lines=lines,
        raw=raw,
    )


def strip_enum(line: str) -> str:
    """去掉行首编号，用于判断标题/正文，不改变原文坐标。"""
    return ANY_ENUM.sub("", line, count=1).lstrip(" 　.、．:：")
