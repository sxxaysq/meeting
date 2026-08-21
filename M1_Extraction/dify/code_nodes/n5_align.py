# -*- coding: utf-8 -*-
"""Dify Code 节点 5：Evidence Aligner。

LLM 只给 evidence_text；start_char / end_char / page_start / page_end /
exact_match 全部在这里由确定性代码算出。绝不伪造 exact_match=true：
只有 evidence_text 的每个有效字符都在原文中连续出现，才返回 true。

注意：Dify 的 Document Extractor 只给纯文本、不给分页信息，
因此这条链路上 page_start/page_end 恒为 null
（与现有人工标注一致）。需要真实页码时走 src/cli.py，
它用 PyMuPDF 逐页读取并保留页字符区间。

输入：candidates_json、doc_text、blocks
输出：items_json、exact_match_count
"""

import json
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
ANCHOR_CHARS = 12
MIN_ANCHOR = 4


def reduce_text(text, base_offset):
    """逐行去掉行首编号、去掉空白，返回归约串与每个字符的绝对偏移。"""
    chars = []
    offsets = []
    cursor = 0
    for line in text.split("\n"):
        match = ANY_ENUM.match(line)
        start = match.end() if match else 0
        while start < len(line) and line[start] in " 　.、．":
            start += 1
        for index in range(start, len(line)):
            char = line[index]
            if not char.isspace():
                chars.append(char)
                offsets.append(base_offset + cursor + index)
        cursor += len(line) + 1
    return "".join(chars), offsets


def build_lines(doc_text):
    lines = []
    position = 0
    for unit in doc_text.split("\n"):
        lines.append({"start": position, "end": position + len(unit)})
        position += len(unit) + 1
    return lines


def line_at(lines, offset):
    for line in lines:
        if line["start"] <= offset < line["end"]:
            return line
        if line["start"] > offset:
            break
    return None


def snap_to_line_edges(doc_text, lines, start, end):
    first = line_at(lines, start)
    if first is not None and start > first["start"]:
        prefix = doc_text[first["start"] : start]
        match = ANY_ENUM.match(prefix)
        if match and not prefix[match.end() :].strip(" 　.、．:："):
            start = first["start"]
    last = line_at(lines, max(start, end - 1))
    if last is not None and end < last["end"] and not doc_text[end : last["end"]].strip():
        end = last["end"]
    return start, end


def align(doc_text, lines, block, evidence_text):
    block_start = int(block.get("start_char") or 0)
    block_end = int(block.get("end_char") or len(doc_text))
    region = doc_text[block_start:block_end]
    evidence_text = (evidence_text or "").strip()

    def finish(start, end, exact):
        start, end = snap_to_line_edges(doc_text, lines, start, end)
        start = max(block_start, min(start, block_end))
        end = max(start, min(end, block_end))
        return start, end, exact

    if not evidence_text:
        return finish(block_start, block_end, False)

    index = region.find(evidence_text)
    if index >= 0:
        return finish(block_start + index, block_start + index + len(evidence_text), True)

    reduced_region, region_offsets = reduce_text(region, block_start)
    reduced_evidence, _ = reduce_text(evidence_text, 0)
    if reduced_evidence:
        index = reduced_region.find(reduced_evidence)
        if index >= 0:
            start = region_offsets[index]
            end = region_offsets[index + len(reduced_evidence) - 1] + 1
            return finish(start, end, True)

    head = reduced_evidence[:ANCHOR_CHARS]
    tail = reduced_evidence[-ANCHOR_CHARS:]
    head_index = reduced_region.find(head) if len(head) >= MIN_ANCHOR else -1
    tail_index = reduced_region.rfind(tail) if len(tail) >= MIN_ANCHOR else -1
    if head_index >= 0 and tail_index >= head_index:
        start = region_offsets[head_index]
        end = region_offsets[min(tail_index + len(tail), len(region_offsets)) - 1] + 1
        return finish(start, end, False)
    if head_index >= 0:
        start = region_offsets[head_index]
        line = line_at(lines, start)
        return finish(start, line["end"] if line else block_end, False)
    return finish(block_start, block_end, False)


def main(candidates_json: str, doc_text: str, blocks: list) -> dict:
    candidates = json.loads(candidates_json or "[]")
    by_index = {int(b.get("index", i)): b for i, b in enumerate(blocks or [])}
    lines = build_lines(doc_text or "")

    items = []
    exact_count = 0
    for candidate in candidates:
        block = by_index.get(int(candidate.get("block_index", -1))) or {
            "start_char": 0,
            "end_char": len(doc_text or ""),
        }
        evidence_text = candidate.get("evidence_text") or candidate.get("content") or ""
        start, end, exact = align(doc_text or "", lines, block, evidence_text)
        if exact:
            exact_count += 1
        items.append(
            {
                "department": candidate.get("department") or block.get("department"),
                "work_section": candidate.get("work_section") or block.get("work_section"),
                "delivery_group": candidate.get("delivery_group")
                or block.get("delivery_group"),
                "project": candidate.get("project"),
                "item_type": candidate.get("item_type"),
                "assignee": candidate.get("assignee") or [],
                "title": candidate.get("title"),
                "content": candidate.get("content"),
                "evidence": {
                    "text": evidence_text,
                    "page_start": block.get("page_start"),
                    "page_end": block.get("page_end"),
                    "start_char": start,
                    "end_char": end,
                    "exact_match": exact,
                },
            }
        )

    return {
        "items_json": json.dumps({"items": items}, ensure_ascii=False),
        "exact_match_count": exact_count,
        "item_count": len(items),
    }
