# -*- coding: utf-8 -*-
"""Dify Code 节点 1：文本规范化 + page/char metadata + 结构切分。

对应 src/text_normalizer.py 与 src/structure_segmenter.py，
在 Dify 沙箱里只能用标准库，因此这里是自包含实现。
tests/test_dify_nodes.py 会断言它与 src/ 主实现产出一致。

输入：doc_text（Document Extractor 的输出）
输出：doc_text（规范化后的正文，后续节点的坐标系）、blocks
"""

import re

ANY_ENUM = re.compile(
    r"^(（[一二三四五六七八九十]+）"
    r"|[一二三四五六七八九十]+[、．]"
    r"|\d+\.\d+"
    r"|\d+[\.、．](?!\d)"
    r"|[（(]\d+[）)]"
    r"|\d+）"
    r"|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])"
)
PAGE_NUMBER = re.compile(r"\n?—\s*\d+\s*—\n?")
INLINE_SECTION = re.compile(r"[一二三四五六七八九十]+[\.、．][^，。；：]{2,18}[：:]\s*$")
LEVELS = [
    (0, re.compile(r"^([一二三四五六七八九十]+)[、．]")),
    (1, re.compile(r"^（([一二三四五六七八九十]+)）")),
    (2, re.compile(r"^(\d+\.\d+)")),
    (2, re.compile(r"^(\d+)[\.、．](?!\d)")),
    (3, re.compile(r"^[（(](\d+)[）)]")),
    (4, re.compile(r"^(\d+)）")),
    (5, re.compile(r"^([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])")),
]
TOP_CHAPTERS = ("各部门本周重点工作", "下一步工作要求", "上周重点工作", "其他事项")
GROUP_SUFFIX = ("交付组", "事业部", "项目组", "分公司", "研究院", "设计院", "中心", "总院", "公司", "部")
HEADING_MAX_CHARS = 26


def normalize_text(text):
    """返回 (规范化文本, 条目行列表)。条目行 = (text, start, end)。"""
    text = PAGE_NUMBER.sub("\n", text or "").replace("　", " ")
    units = []
    current = ""
    for physical in text.split("\n"):
        line = physical.strip()
        if not line:
            continue
        if ANY_ENUM.match(line):
            if current:
                units.append(current)
            current = line
        else:
            current += line
    if current:
        units.append(current)

    normalized = "\n".join(units)
    lines = []
    position = 0
    for unit in units:
        lines.append({"text": unit, "start": position, "end": position + len(unit)})
        position += len(unit) + 1
    return normalized, lines


def strip_enum(line):
    return ANY_ENUM.sub("", line, count=1).lstrip(" 　.、．:：")


def level_of(line, seen_department):
    for level, pattern in LEVELS:
        if pattern.match(line):
            if level == 0 and seen_department and not any(t in line[:16] for t in TOP_CHAPTERS):
                return 2
            return level
    return None


def is_heading(body):
    if not body:
        return True
    if len(body) > HEADING_MAX_CHARS:
        return False
    if re.search(r"[。；;！？]", body):
        return False
    stripped = body.rstrip("：: ")
    return not stripped or not re.search(r"[，,]", stripped)


def heading_text(body):
    return body.rstrip("：: 　").strip()


def split_inline_section(body):
    match = INLINE_SECTION.search(body)
    if not match or match.start() == 0:
        return body, None
    return body[: match.start()], heading_text(match.group(0))


def segment(normalized, lines, max_block_chars):
    blocks = []
    buffer = []
    context = {"department": None, "work_section": None, "delivery_group": None}
    seen_department = False

    def flush():
        if not buffer:
            return
        start = buffer[0]["start"]
        end = buffer[-1]["end"]
        blocks.append(
            {
                "index": len(blocks),
                "department": context["department"],
                "work_section": context["work_section"],
                "delivery_group": context["delivery_group"],
                "raw_text": normalized[start:end],
                "start_char": start,
                "end_char": end,
                "page_start": None,
                "page_end": None,
            }
        )
        del buffer[:]

    for line in lines:
        text = line["text"]
        level = level_of(text, seen_department)
        body = strip_enum(text) if level is not None else text
        body, trailing = split_inline_section(body)
        heading = is_heading(body)

        if level == 0:
            flush()
            context.update(department=None, work_section=None, delivery_group=None)
            continue
        if level == 1 and heading:
            flush()
            context.update(
                department=heading_text(body),
                work_section=trailing,
                delivery_group=None,
            )
            seen_department = True
            continue
        if level == 2 and heading:
            # 板块名与行尾粘连的子标题都属于板块名，丢掉后半段会让归属信息失真
            flush()
            context.update(
                work_section=heading_text(body) + (trailing or ""), delivery_group=None
            )
            continue
        if level == 2 and not heading:
            head, inline = split_inline_section(body)
            if inline:
                flush()
                context.update(
                    work_section=heading_text(head) + inline, delivery_group=None
                )
                continue
        if level == 3 and heading and heading_text(body).endswith(GROUP_SUFFIX):
            flush()
            context.update(delivery_group=heading_text(body))
            continue

        if (
            level is not None
            and level <= 4
            and buffer
            and (buffer[-1]["end"] - buffer[0]["start"]) >= max_block_chars
        ):
            flush()
        buffer.append(line)

        if trailing:
            flush()
            context.update(work_section=trailing, delivery_group=None)

    flush()
    return blocks


def main(doc_text: str, max_block_chars: str) -> dict:
    try:
        cap = int(max_block_chars)
    except (TypeError, ValueError):
        cap = 1200
    normalized, lines = normalize_text(doc_text)
    blocks = segment(normalized, lines, cap)
    return {
        "doc_text": normalized,
        "blocks": blocks,
        "block_count": len(blocks),
    }
