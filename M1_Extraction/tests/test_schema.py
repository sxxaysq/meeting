# -*- coding: utf-8 -*-
"""M1 输出 Schema 的严格性测试。"""

import copy

import pytest
from conftest import *  # noqa: F401,F403 - 注入 src 到 sys.path

from schema_validator import SchemaError, validate_items

VALID = {
    "items": [
        {
            "department": "智能矿山事业部",
            "work_section": "项目推进",
            "delivery_group": "新疆交付组",
            "project": "红沙泉二矿项目",
            "item_type": "PROJECT_TASK",
            "assignee": [],
            "title": "集控中心建设",
            "content": "完成顶面线管、桥架安装。",
            "evidence": {
                "text": "①集控中心（四合院）：完成顶面线管、桥架安装。",
                "page_start": None,
                "page_end": None,
                "start_char": 380,
                "end_char": 431,
                "exact_match": True,
            },
        }
    ]
}


def test_valid_payload_passes():
    assert validate_items(copy.deepcopy(VALID), strict=False) == []


@pytest.mark.parametrize(
    "field",
    [
        "department",
        "work_section",
        "delivery_group",
        "project",
        "item_type",
        "assignee",
        "title",
        "content",
        "evidence",
    ],
)
def test_every_field_is_required(field):
    payload = copy.deepcopy(VALID)
    del payload["items"][0][field]
    assert validate_items(payload, strict=False), "缺少 {} 应当报错".format(field)


@pytest.mark.parametrize(
    "field",
    ["confidence", "priority", "project_id", "task_id", "reasoning", "merge_group"],
)
def test_forbidden_business_fields_rejected(field):
    payload = copy.deepcopy(VALID)
    payload["items"][0][field] = "x"
    errors = validate_items(payload, strict=False)
    assert any(field in e for e in errors), "{} 属于禁止字段".format(field)


def test_assignee_must_be_array_not_null():
    payload = copy.deepcopy(VALID)
    payload["items"][0]["assignee"] = None
    assert validate_items(payload, strict=False)


def test_assignee_empty_array_is_valid():
    payload = copy.deepcopy(VALID)
    payload["items"][0]["assignee"] = []
    assert validate_items(payload, strict=False) == []


@pytest.mark.parametrize(
    "item_type",
    ["PROJECT_TASK", "RESEARCH_TASK", "NON_PROJECT_WORK", "NON_TASK_ITEM"],
)
def test_four_legal_item_types(item_type):
    payload = copy.deepcopy(VALID)
    payload["items"][0]["item_type"] = item_type
    assert validate_items(payload, strict=False) == []


@pytest.mark.parametrize("item_type", ["CREATE", "UPDATE", "TASK", "", None])
def test_illegal_item_type_rejected(item_type):
    payload = copy.deepcopy(VALID)
    payload["items"][0]["item_type"] = item_type
    assert validate_items(payload, strict=False)


def test_nullable_context_fields():
    payload = copy.deepcopy(VALID)
    for field in ("department", "work_section", "delivery_group", "project"):
        payload["items"][0][field] = None
    assert validate_items(payload, strict=False) == []


def test_non_task_item_with_project_is_legal():
    """project 与 item_type 是两个独立维度。"""
    payload = copy.deepcopy(VALID)
    payload["items"][0].update(
        {"project": "武家塔项目", "item_type": "NON_TASK_ITEM", "content": "暂无。"}
    )
    assert validate_items(payload, strict=False) == []


def test_extra_toplevel_key_rejected():
    payload = copy.deepcopy(VALID)
    payload["source_file"] = "x.pdf"
    assert validate_items(payload, strict=False)


def test_evidence_requires_all_six_fields():
    payload = copy.deepcopy(VALID)
    del payload["items"][0]["evidence"]["exact_match"]
    assert validate_items(payload, strict=False)


def test_empty_title_rejected():
    payload = copy.deepcopy(VALID)
    payload["items"][0]["title"] = "  "
    assert validate_items(payload, strict=False)


def test_strict_mode_raises():
    payload = copy.deepcopy(VALID)
    payload["items"][0]["item_type"] = "CREATE"
    with pytest.raises(SchemaError):
        validate_items(payload, strict=True)
