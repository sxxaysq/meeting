# -*- coding: utf-8 -*-
"""业务结构切分：把规范化后的条目行还原成"部门 / 工作板块 / 交付组"层级 Block。

这一步是确定性的，理由有两条：
  1. 会议纪要的编号层级本身就是业务层级，正则可以 100% 复现；
  2. Block 需要精确的 start_char/end_char，LLM 不允许猜测字符位置。

切分保证（对应需求第六节）：
  * 不从项目描述内部切断 —— 只在条目行边界切；
  * 不从负责人与其任务之间切断 —— 项目行与其 ①②③ 动作行始终同块；
  * 一个 Block 可以包含多个项目；
  * Block 保留原始文本与原文位置。

文档没有可识别编号层级时（例如自由排版的 DOCX），
调用方可以改用 prompts/structure_segmenter.md 里的 LLM 兜底切分。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from text_normalizer import ANY_ENUM, NormalizedDocument, UnitLine, strip_enum

# 层级标号：数字越小越靠外
LEVELS = [
    (0, re.compile(r"^([一二三四五六七八九十]+)[、．]")),
    (1, re.compile(r"^（([一二三四五六七八九十]+)）")),
    (2, re.compile(r"^(\d+\.\d+)")),
    (2, re.compile(r"^(\d+)[\.、．](?!\d)")),
    (3, re.compile(r"^[（(](\d+)[）)]")),
    (4, re.compile(r"^(\d+)）")),
    (5, re.compile(r"^([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])")),
]

# 顶层大章标题，用来区分"一、"是大章还是部门内部的小节编号
TOP_CHAPTERS = ("各部门本周重点工作", "下一步工作要求", "上周重点工作", "其他事项")

# 交付组 / 单位型小标题
GROUP_SUFFIX = (
    "交付组", "事业部", "项目组", "分公司", "研究院", "设计院", "中心", "总院", "公司", "部",
)

# 行内混排的小节标题，例如 "……二.重点项目实施推进："
INLINE_SECTION = re.compile(r"[一二三四五六七八九十]+[\.、．][^，。；：]{2,18}[：:]\s*$")

MAX_BLOCK_CHARS = 1200
HEADING_MAX_CHARS = 26


@dataclass
class Block:
    index: int
    department: Optional[str]
    work_section: Optional[str]
    delivery_group: Optional[str]
    raw_text: str
    start_char: int
    end_char: int
    page_start: Optional[int]
    page_end: Optional[int]

    def to_dict(self) -> dict:
        return asdict(self)


def _level_of(line: str, seen_department: bool) -> Optional[int]:
    for level, pattern in LEVELS:
        if pattern.match(line):
            if level == 0 and seen_department and not any(t in line[:16] for t in TOP_CHAPTERS):
                # 部门内部复用 "一、""二、" 做小节，不是顶层大章
                return 2
            return level
    return None


def _is_heading(body: str) -> bool:
    """判断去掉编号后的内容是不是纯标题（没有实际工作内容）。"""
    if not body:
        return True
    if len(body) > HEADING_MAX_CHARS:
        return False
    # 带句末标点说明是正文
    if re.search(r"[。；;！？]", body):
        return False
    stripped = body.rstrip("：: ")
    if not stripped:
        return True
    # "新疆交付组" / "集团总部" 这类；也允许 "1.经营工作" 这种无冒号短标题
    return not re.search(r"[，,]", stripped)


def _heading_text(body: str) -> str:
    return body.rstrip("：: 　").strip()


def _looks_like_group(text: str) -> bool:
    return text.endswith(GROUP_SUFFIX)


def _split_inline_section(body: str):
    """把粘在行尾的小节标题拆出来，例如 "……汇报。二.重点项目实施推进："。"""
    match = INLINE_SECTION.search(body)
    if not match or match.start() == 0:
        return body, None
    return body[: match.start()], _heading_text(match.group(0))


class _BlockBuilder:
    def __init__(self, doc: NormalizedDocument, max_block_chars: int):
        self.doc = doc
        self.max_block_chars = max_block_chars
        self.blocks: List[Block] = []
        self.lines: List[UnitLine] = []
        self.department: Optional[str] = None
        self.work_section: Optional[str] = None
        self.delivery_group: Optional[str] = None

    def flush(self) -> None:
        if not self.lines:
            return
        start = self.lines[0].start
        end = self.lines[-1].end
        page_start, page_end = self.doc.page_range(start, end)
        self.blocks.append(
            Block(
                index=len(self.blocks),
                department=self.department,
                work_section=self.work_section,
                delivery_group=self.delivery_group,
                raw_text=self.doc.text[start:end],
                start_char=start,
                end_char=end,
                page_start=page_start,
                page_end=page_end,
            )
        )
        self.lines = []

    def size(self) -> int:
        if not self.lines:
            return 0
        return self.lines[-1].end - self.lines[0].start

    def add(self, line: UnitLine, level: Optional[int]) -> None:
        # ①②③ 动作行必须跟随上一行项目，不允许在此处断开
        safe_boundary = level is not None and level <= 4
        if safe_boundary and self.size() >= self.max_block_chars:
            self.flush()
        self.lines.append(line)


def segment_blocks(doc: NormalizedDocument, max_block_chars: int = MAX_BLOCK_CHARS) -> List[Block]:
    builder = _BlockBuilder(doc, max_block_chars)
    seen_department = False

    for line in doc.lines:
        text = line.text
        level = _level_of(text, seen_department)
        body = strip_enum(text) if level is not None else text
        body, trailing_section = _split_inline_section(body)
        heading = _is_heading(body)

        if level == 0:
            # 顶层大章：重置全部上下文
            builder.flush()
            builder.department = None
            builder.work_section = None
            builder.delivery_group = None
            continue

        if level == 1 and heading:
            builder.flush()
            builder.department = _heading_text(body)
            # "（二）数字化技术服务事业部一.售前项目跟进：" 行尾挂着的小节立即生效
            builder.work_section = trailing_section
            builder.delivery_group = None
            seen_department = True
            continue

        if level == 2 and heading:
            builder.flush()
            # "1.集团总部及子企业一.售前项目跟进：" 是"板块+子标题"粘连，
            # 两段都属于板块名，丢掉后半段会让归属信息失真
            builder.work_section = _heading_text(body) + (trailing_section or "")
            builder.delivery_group = None
            continue

        if level == 2 and not heading:
            # "1.集团总部及子企业一.售前项目跟进：" —— 标题与正文粘在一起
            head, inline = _split_inline_section(body)
            if inline:
                builder.flush()
                builder.work_section = _heading_text(head) + inline
                builder.delivery_group = None
                continue

        if level == 3 and heading and _looks_like_group(_heading_text(body)):
            builder.flush()
            builder.delivery_group = _heading_text(body)
            continue

        builder.add(line, level)

        if trailing_section:
            # 行尾挂着的下一节标题，在本行之后生效
            builder.flush()
            builder.work_section = trailing_section
            builder.delivery_group = None

    builder.flush()
    for index, block in enumerate(builder.blocks):
        block.index = index
    return builder.blocks


def blocks_to_payload(blocks: List[Block]) -> dict:
    return {"blocks": [b.to_dict() for b in blocks]}
