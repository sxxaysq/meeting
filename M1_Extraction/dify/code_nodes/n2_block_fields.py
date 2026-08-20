# -*- coding: utf-8 -*-
"""Dify Code 节点 2（Iteration 内）：把当前 Block 对象拆成提示词需要的字符串。

LLM 节点的提示词模板只能插入标量，所以这里先把 object 展开。

输入：block（iteration 的 item）
输出：department / work_section / delivery_group / raw_text / block_index
"""


def main(block: dict) -> dict:
    block = block or {}
    return {
        "department": block.get("department") or "（未标注）",
        "work_section": block.get("work_section") or "（未标注）",
        "delivery_group": block.get("delivery_group") or "（未标注）",
        "raw_text": block.get("raw_text") or "",
        "block_index": int(block.get("index") or 0),
    }
