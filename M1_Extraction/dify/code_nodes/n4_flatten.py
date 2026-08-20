# -*- coding: utf-8 -*-
"""Dify Code 节点 4：Flatten。

把 Iteration 汇总出的 array[string] 展平成一个候选 Item 列表，
并把各轮的 dropped / error 汇总起来。

输入：rounds（array[string]，每轮一个 JSON 字符串）
输出：candidates_json、dropped_json、block_errors_json、candidate_count
"""

import json


def main(rounds: list) -> dict:
    candidates = []
    dropped = []
    warnings = []
    errors = []

    for entry in rounds or []:
        if isinstance(entry, str):
            try:
                payload = json.loads(entry)
            except ValueError:
                errors.append({"block_index": -1, "error": "本轮输出不是 JSON"})
                continue
        elif isinstance(entry, dict):
            payload = entry
        else:
            continue

        block_index = payload.get("block_index", -1)
        for item in payload.get("items") or []:
            candidates.append(item)
        for item in payload.get("dropped") or []:
            item = dict(item)
            item["block_index"] = block_index
            dropped.append(item)
        for item in payload.get("warnings") or []:
            item = dict(item)
            item["block_index"] = block_index
            warnings.append(item)
        if payload.get("error"):
            errors.append({"block_index": block_index, "error": payload["error"]})

    return {
        "candidates_json": json.dumps(candidates, ensure_ascii=False),
        "dropped_json": json.dumps(dropped, ensure_ascii=False),
        "normalize_warnings_json": json.dumps(warnings, ensure_ascii=False),
        "block_errors_json": json.dumps(errors, ensure_ascii=False),
        "candidate_count": len(candidates),
    }
