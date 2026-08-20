# -*- coding: utf-8 -*-
"""Dify Code 节点 3（Iteration 内）：解析 LLM 输出，附上 block_index。

只做 JSON 解析和结构归一，不做语义修补。
模型给出非法 item_type 等问题时，该候选进入 dropped 并带上原因，
不会被悄悄改成看起来合理的值。

输入：llm_text（LLM 节点原始输出）、block_index
输出：round_json（本轮结果的 JSON 字符串，交给 Iteration 汇总）
"""

import json
import re

ITEM_TYPES = ("PROJECT_TASK", "RESEARCH_TASK", "NON_PROJECT_WORK", "NON_TASK_ITEM")
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
MAX_ASSIGNEE_CHARS = 8


def extract_json(content):
    if not content:
        return None
    content = THINK_BLOCK.sub("", content).strip()
    fenced = JSON_FENCE.search(content)
    if fenced:
        content = fenced.group(1).strip()
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(content[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def clean_optional(value):
    if isinstance(value, str):
        return value.strip() or None
    return None


def clean_assignee(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace("、", "/").split("/")
    if not isinstance(value, list):
        return None
    names = []
    for entry in value:
        if not isinstance(entry, str):
            return None
        name = entry.strip()
        if not name:
            continue
        if len(name) > MAX_ASSIGNEE_CHARS:
            return None
        if name not in names:
            names.append(name)
    return names


def main(llm_text: str, block_index: int) -> dict:
    payload = extract_json(llm_text)
    items = []
    dropped = []
    warnings = []
    error = ""

    if payload is None:
        error = "模型未返回合法 JSON"
    elif not isinstance(payload.get("items"), list):
        error = "模型输出缺少 items 数组"
    else:
        for position, raw in enumerate(payload["items"]):
            if not isinstance(raw, dict):
                dropped.append({"position": position, "reason": "不是对象"})
                continue
            item_type = raw.get("item_type")
            title = clean_optional(raw.get("title"))
            content = clean_optional(raw.get("content"))
            assignee = clean_assignee(raw.get("assignee"))
            if item_type not in ITEM_TYPES:
                dropped.append(
                    {"position": position, "reason": "非法 item_type: %r" % (item_type,)}
                )
                continue
            if not title or not content:
                dropped.append({"position": position, "reason": "title 或 content 为空"})
                continue
            if assignee is None:
                # 只有单个附属字段不对时清空该字段并告警，不丢掉整条业务事项
                warnings.append(
                    {
                        "position": position,
                        "detail": "assignee 形状非法已清空: %r" % (raw.get("assignee"),),
                    }
                )
                assignee = []
            evidence_text = clean_optional(raw.get("evidence_text"))
            if not evidence_text and isinstance(raw.get("evidence"), dict):
                evidence_text = clean_optional(raw["evidence"].get("text"))
            items.append(
                {
                    "block_index": int(block_index),
                    "department": clean_optional(raw.get("department")),
                    "work_section": clean_optional(raw.get("work_section")),
                    "delivery_group": clean_optional(raw.get("delivery_group")),
                    "project": clean_optional(raw.get("project")),
                    "item_type": item_type,
                    "assignee": assignee,
                    "title": title,
                    "content": content,
                    "evidence_text": evidence_text or content,
                }
            )

    return {
        "round_json": json.dumps(
            {
                "block_index": int(block_index),
                "items": items,
                "dropped": dropped,
                "warnings": warnings,
                "error": error,
            },
            ensure_ascii=False,
        )
    }
