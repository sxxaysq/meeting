# -*- coding: utf-8 -*-
"""测试夹具。Item 判定全部走假模型替身，测试不依赖在线模型。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))


class FakeClient:
    """按调用顺序或按规则返回预设 JSON。

    ``rules`` 是 (谓词, 返回值) 列表；谓词收到 (system, user) 两个字符串。
    没有命中规则时返回 ``default``。
    """

    def __init__(self, rules=None, default=None):
        self.rules = rules or []
        self.default = default
        self.calls = []

    def complete_json(self, system, user, retries=2):
        self.calls.append((system, user))
        for predicate, value in self.rules:
            if predicate(system, user):
                return value
        return self.default

    def stats(self):
        return {"model": "fake", "calls": len(self.calls)}


def make_item(
    index=0,
    department="智能矿山事业部",
    work_section="项目推进",
    delivery_group="新疆交付组",
    project="红沙泉二矿项目",
    item_type="PROJECT_TASK",
    assignee=None,
    title="集控中心建设",
    content="完成顶面线管、桥架安装100%。",
    start=0,
    end=None,
    text=None,
    exact_match=True,
):
    body = text if text is not None else content
    end = end if end is not None else start + len(body)
    return {
        "department": department,
        "work_section": work_section,
        "delivery_group": delivery_group,
        "project": project,
        "item_type": item_type,
        "assignee": list(assignee or []),
        "title": title,
        "content": content,
        "evidence": {
            "text": body,
            "page_start": 1,
            "page_end": 1,
            "start_char": start,
            "end_char": end,
            "exact_match": exact_match,
        },
    }


@pytest.fixture
def fake_client_factory():
    return FakeClient


@pytest.fixture
def item_factory():
    return make_item
