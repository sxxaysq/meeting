# -*- coding: utf-8 -*-
"""M1 输出的严格 Schema 校验。

失败时不静默修正成"看起来正常"的结果，而是把错误原样抛出来，
由调用方决定进入 retry / fallback 还是直接失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
ITEM_FIELDS = (
    "department",
    "work_section",
    "delivery_group",
    "project",
    "item_type",
    "assignee",
    "title",
    "content",
    "evidence",
)
EVIDENCE_FIELDS = (
    "text",
    "page_start",
    "page_end",
    "start_char",
    "end_char",
    "exact_match",
)
ITEM_TYPES = ("PROJECT_TASK", "RESEARCH_TASK", "NON_PROJECT_WORK", "NON_TASK_ITEM")


class SchemaError(ValueError):
    pass


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _validate_with_jsonschema(payload: dict, schema_name: str) -> List[str]:
    try:
        import jsonschema
    except ImportError:
        return []
    validator = jsonschema.Draft7Validator(load_schema(schema_name))
    return [
        "{}: {}".format("/".join(str(p) for p in error.path) or "<root>", error.message)
        for error in validator.iter_errors(payload)
    ]


def _validate_items_builtin(payload: dict) -> List[str]:
    """不依赖 jsonschema 的等价检查，保证 Dify Code 节点里也能跑。"""
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["<root>: 顶层必须是对象"]
    if set(payload) != {"items"}:
        errors.append("<root>: 顶层只允许 items 一个键，实际为 {}".format(sorted(payload)))
    items = payload.get("items")
    if not isinstance(items, list):
        return errors + ["items: 必须是数组"]

    for index, item in enumerate(items):
        prefix = "items/{}".format(index)
        if not isinstance(item, dict):
            errors.append("{}: 必须是对象".format(prefix))
            continue
        extra = set(item) - set(ITEM_FIELDS)
        if extra:
            errors.append("{}: 出现非法字段 {}".format(prefix, sorted(extra)))
        missing = set(ITEM_FIELDS) - set(item)
        if missing:
            errors.append("{}: 缺少字段 {}".format(prefix, sorted(missing)))
            continue
        for field in ("department", "work_section", "delivery_group", "project"):
            if item[field] is not None and not isinstance(item[field], str):
                errors.append("{}/{}: 必须是 string 或 null".format(prefix, field))
        if item["item_type"] not in ITEM_TYPES:
            errors.append("{}/item_type: 非法值 {!r}".format(prefix, item["item_type"]))
        if not isinstance(item["assignee"], list) or not all(
            isinstance(a, str) and a for a in item["assignee"]
        ):
            errors.append("{}/assignee: 必须是字符串数组（无人时为 []，不能是 null）".format(prefix))
        for field in ("title", "content"):
            if not isinstance(item[field], str) or not item[field].strip():
                errors.append("{}/{}: 必须是非空字符串".format(prefix, field))
        evidence = item["evidence"]
        if not isinstance(evidence, dict):
            errors.append("{}/evidence: 必须是对象".format(prefix))
            continue
        if set(evidence) != set(EVIDENCE_FIELDS):
            errors.append(
                "{}/evidence: 字段必须恰好是 {}，实际 {}".format(
                    prefix, list(EVIDENCE_FIELDS), sorted(evidence)
                )
            )
            continue
        if not isinstance(evidence["text"], str) or not evidence["text"].strip():
            errors.append("{}/evidence/text: 必须是非空字符串".format(prefix))
        for field in ("start_char", "end_char"):
            if not isinstance(evidence[field], int) or isinstance(evidence[field], bool):
                errors.append("{}/evidence/{}: 必须是整数".format(prefix, field))
        for field in ("page_start", "page_end"):
            value = evidence[field]
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                errors.append("{}/evidence/{}: 必须是整数或 null".format(prefix, field))
        if not isinstance(evidence["exact_match"], bool):
            errors.append("{}/evidence/exact_match: 必须是布尔值".format(prefix))
        if (
            isinstance(evidence.get("start_char"), int)
            and isinstance(evidence.get("end_char"), int)
            and evidence["end_char"] < evidence["start_char"]
        ):
            errors.append("{}/evidence: end_char 小于 start_char".format(prefix))
    return errors


def validate_items(payload: dict, strict: bool = True) -> List[str]:
    errors = _validate_items_builtin(payload)
    errors.extend(_validate_with_jsonschema(payload, "m1_items.schema.json"))
    # 两条路径可能报同一个问题，去重后保持稳定顺序
    seen = set()
    unique = [e for e in errors if not (e in seen or seen.add(e))]
    if unique and strict:
        raise SchemaError("M1 输出不符合 Schema：\n- " + "\n- ".join(unique))
    return unique


def validate_blocks(payload: dict, strict: bool = True) -> List[str]:
    errors = _validate_with_jsonschema(payload, "blocks.schema.json")
    if errors and strict:
        raise SchemaError("blocks 不符合 Schema：\n- " + "\n- ".join(errors))
    return errors
