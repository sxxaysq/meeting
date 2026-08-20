# -*- coding: utf-8 -*-
"""Dify Code 节点 7：Final JSON Schema Validator。

严格校验 schemas/m1_items.schema.json 约定的结构：
9 个字段全部必需、item_type 四选一、assignee 必须是数组、
禁止 additionalProperties。

校验失败时**不**输出一个"看起来正常"的结果，而是直接抛异常让整个
workflow 显式失败——把不合规的结果悄悄交给 M2 比报错危险得多。

输入：items_json
输出：items（array[object]，最终业务输出）、item_count
"""

import json

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
NULLABLE_STRINGS = ("department", "work_section", "delivery_group", "project")


def validate(payload):
    errors = []
    if not isinstance(payload, dict):
        return ["<root>: 顶层必须是对象"]
    if set(payload) != {"items"}:
        errors.append("<root>: 顶层只允许 items 一个键，实际为 %s" % (sorted(payload),))
    items = payload.get("items")
    if not isinstance(items, list):
        return errors + ["items: 必须是数组"]

    for index, item in enumerate(items):
        prefix = "items/%d" % index
        if not isinstance(item, dict):
            errors.append("%s: 必须是对象" % prefix)
            continue
        extra = set(item) - set(ITEM_FIELDS)
        if extra:
            errors.append("%s: 出现非法字段 %s" % (prefix, sorted(extra)))
        missing = set(ITEM_FIELDS) - set(item)
        if missing:
            errors.append("%s: 缺少字段 %s" % (prefix, sorted(missing)))
            continue
        for field in NULLABLE_STRINGS:
            if item[field] is not None and not isinstance(item[field], str):
                errors.append("%s/%s: 必须是 string 或 null" % (prefix, field))
        if item["item_type"] not in ITEM_TYPES:
            errors.append("%s/item_type: 非法值 %r" % (prefix, item["item_type"]))
        if not isinstance(item["assignee"], list) or not all(
            isinstance(a, str) and a for a in item["assignee"]
        ):
            errors.append("%s/assignee: 必须是字符串数组（无人时为 []，不能是 null）" % prefix)
        for field in ("title", "content"):
            if not isinstance(item[field], str) or not item[field].strip():
                errors.append("%s/%s: 必须是非空字符串" % (prefix, field))
        evidence = item["evidence"]
        if not isinstance(evidence, dict):
            errors.append("%s/evidence: 必须是对象" % prefix)
            continue
        if set(evidence) != set(EVIDENCE_FIELDS):
            errors.append(
                "%s/evidence: 字段必须恰好是 %s，实际 %s"
                % (prefix, list(EVIDENCE_FIELDS), sorted(evidence))
            )
            continue
        if not isinstance(evidence["text"], str) or not evidence["text"].strip():
            errors.append("%s/evidence/text: 必须是非空字符串" % prefix)
        for field in ("start_char", "end_char"):
            if not isinstance(evidence[field], int) or isinstance(evidence[field], bool):
                errors.append("%s/evidence/%s: 必须是整数" % (prefix, field))
        for field in ("page_start", "page_end"):
            value = evidence[field]
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                errors.append("%s/evidence/%s: 必须是整数或 null" % (prefix, field))
        if not isinstance(evidence["exact_match"], bool):
            errors.append("%s/evidence/exact_match: 必须是布尔值" % prefix)
    return errors


def main(items_json: str) -> dict:
    try:
        payload = json.loads(items_json or '{"items":[]}')
    except ValueError as error:
        raise ValueError("M1 输出不是合法 JSON：%s" % error)

    errors = validate(payload)
    if errors:
        raise ValueError("M1 输出不符合 Schema：\n- " + "\n- ".join(errors))

    items = payload["items"]
    return {"items": items, "item_count": len(items)}
