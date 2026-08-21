# -*- coding: utf-8 -*-
"""M2 输出的严格 Schema 校验。

与 M1 一样走双路：内置确定性校验 + 可选 jsonschema。
内置校验不依赖第三方包，保证最小环境也能跑；装了 jsonschema 时两边都跑，
任何一边报错都算违规。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from models import ITEM_TYPES, SOURCE_MODES

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "m2_output.schema.json"

ITEM_KEYS = {
    "department",
    "work_section",
    "delivery_group",
    "project",
    "item_type",
    "assignee",
    "title",
    "content",
    "evidence",
}
EVIDENCE_KEYS = {
    "text",
    "page_start",
    "page_end",
    "start_char",
    "end_char",
    "exact_match",
}
# M1 明令禁止的字段，M2 同样不许在 items[] 里冒出来
FORBIDDEN = {
    "confidence",
    "status",
    "priority",
    "project_id",
    "task_id",
    "merge_group",
    "reasoning",
    "CREATE",
    "UPDATE",
}


def _check_nullable_string(value: Any, path: str, errors: List[str]) -> None:
    if value is not None and not isinstance(value, str):
        errors.append("{} 必须是字符串或 null".format(path))


def validate_item(item: Any, path: str, errors: List[str]) -> None:
    if not isinstance(item, dict):
        errors.append("{} 必须是对象".format(path))
        return
    keys = set(item)
    missing = ITEM_KEYS - keys
    extra = keys - ITEM_KEYS
    if missing:
        errors.append("{} 缺字段：{}".format(path, "、".join(sorted(missing))))
    if extra:
        errors.append("{} 出现多余字段：{}".format(path, "、".join(sorted(extra))))
    for name in FORBIDDEN & keys:
        errors.append("{} 出现禁止字段 {}".format(path, name))

    for name in ("department", "work_section", "delivery_group", "project"):
        _check_nullable_string(item.get(name), "{}.{}".format(path, name), errors)
    if item.get("item_type") not in ITEM_TYPES:
        errors.append("{}.item_type 非法：{!r}".format(path, item.get("item_type")))
    assignee = item.get("assignee")
    if not isinstance(assignee, list) or any(
        not isinstance(a, str) or not a for a in assignee
    ):
        errors.append("{}.assignee 必须是非空字符串数组（无人时为 []）".format(path))
    for name in ("title", "content"):
        value = item.get(name)
        if not isinstance(value, str) or not value:
            errors.append("{}.{} 必须是非空字符串".format(path, name))

    evidence = item.get("evidence")
    if not isinstance(evidence, dict):
        errors.append("{}.evidence 必须是对象".format(path))
        return
    ev_extra = set(evidence) - EVIDENCE_KEYS
    ev_missing = EVIDENCE_KEYS - set(evidence)
    if ev_missing:
        errors.append("{}.evidence 缺字段：{}".format(path, "、".join(sorted(ev_missing))))
    if ev_extra:
        errors.append("{}.evidence 多余字段：{}".format(path, "、".join(sorted(ev_extra))))
    if not isinstance(evidence.get("text"), str) or not evidence.get("text"):
        errors.append("{}.evidence.text 必须是非空字符串".format(path))
    for name in ("start_char", "end_char"):
        value = evidence.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append("{}.evidence.{} 必须是非负整数".format(path, name))
    for name in ("page_start", "page_end"):
        value = evidence.get(name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            errors.append("{}.evidence.{} 必须是整数或 null".format(path, name))
    if not isinstance(evidence.get("exact_match"), bool):
        errors.append("{}.evidence.exact_match 必须是布尔".format(path))


def validate(payload: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["顶层必须是对象"]

    required = {"source_mode", "items", "project_entities", "merge_trace", "validation"}
    missing = required - set(payload)
    extra = set(payload) - required
    if missing:
        errors.append("顶层缺字段：{}".format("、".join(sorted(missing))))
    if extra:
        errors.append("顶层出现多余字段：{}".format("、".join(sorted(extra))))

    if payload.get("source_mode") not in SOURCE_MODES:
        errors.append("source_mode 必须是 block 或 generic")

    items = payload.get("items")
    if not isinstance(items, list):
        errors.append("items 必须是数组")
        items = []
    for index, item in enumerate(items):
        validate_item(item, "items[{}]".format(index), errors)

    trace = payload.get("merge_trace")
    if not isinstance(trace, list):
        errors.append("merge_trace 必须是数组")
    else:
        seen = set()
        for index, entry in enumerate(trace):
            path = "merge_trace[{}]".format(index)
            if not isinstance(entry, dict):
                errors.append("{} 必须是对象".format(path))
                continue
            item_index = entry.get("item_index")
            if not isinstance(item_index, int) or not 0 <= item_index < len(items):
                errors.append("{}.item_index 越界".format(path))
            elif item_index in seen:
                errors.append("{}.item_index 重复".format(path))
            else:
                seen.add(item_index)
            sources = entry.get("source_indexes")
            if not isinstance(sources, list) or not sources:
                errors.append("{}.source_indexes 必须非空".format(path))
            if entry.get("evidence_mode") not in (
                "single",
                "bounding_span",
                "primary_source",
            ):
                errors.append("{}.evidence_mode 非法".format(path))
        # 每条输出 Item 都必须可回溯
        if len(seen) != len(items):
            errors.append(
                "merge_trace 未覆盖全部 items（{}/{}）".format(len(seen), len(items))
            )

    validation = payload.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation 必须是对象")
    elif validation.get("status") not in ("PASS", "REVIEW", "ERROR"):
        errors.append("validation.status 非法")

    errors.extend(_jsonschema_errors(payload))
    return errors


def _jsonschema_errors(payload: Any) -> List[str]:
    try:
        import jsonschema
    except ImportError:
        return []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    return [
        "jsonschema: {} @ {}".format(
            error.message, "/".join(str(p) for p in error.absolute_path) or "<root>"
        )
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ][:20]
