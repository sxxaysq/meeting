# -*- coding: utf-8 -*-
"""m4_bidding 测试公共夹具（假 LLM client，无网络）。"""

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
M4_DIR = HERE.parent
sys.path.insert(0, str(M4_DIR))


def make_item(**overrides):
    """一个完全合法的九字段 item（形态与 m1_staging/tests/conftest.py 一致）。"""
    item = {
        "department": "智能矿山事业部",
        "work_section": "经营工作",
        "delivery_group": None,
        "project": "智能综合管控平台",
        "item_type": "PROJECT_TASK",
        "assignee": ["张三", "李四"],
        "title": "完成管控平台数据接入",
        "content": "本周完成智能综合管控平台的数据接入与联调",
        "evidence": {
            "text": "完成智能综合管控平台的数据接入与联调",
            "page_start": 1,
            "page_end": 1,
            "start_char": 120,
            "end_char": 138,
            "exact_match": True,
        },
    }
    item.update(overrides)
    return item


def make_bidding_item(**overrides):
    """一个明显招投标类的 item（标题/内容含强关键词）。"""
    return make_item(
        title="编制红沙泉二矿项目招标文件",
        content="本周完成红沙泉二矿智能化建设项目招标文件编制并提交招采部门",
        project="红沙泉二矿智能化建设项目",
    )


class FakeLLMClient:
    """按预先给定的 idx → (is_bidding, category) 映射应答，记录调用以供断言。"""

    def __init__(self, decisions, invalid_output=None):
        # decisions: {idx: (is_bidding, category, reason)}；未给的默认 NON_BIDDING
        self.decisions = decisions
        self.invalid_output = invalid_output
        self.calls = 0

    def complete_json(self, system, user, retries=2):
        self.calls += 1
        if self.invalid_output is not None:
            return self.invalid_output
        import re

        indices = [int(m) for m in re.findall(r"\[idx=(\d+)\]", user)]
        results = []
        for idx in indices:
            is_bidding, category, reason = self.decisions.get(
                idx, (False, "NON_BIDDING", "非招投标工作")
            )
            results.append(
                {"idx": idx, "is_bidding": is_bidding, "category": category, "reason": reason}
            )
        return {"results": results}

    def stats(self):
        return {"calls": self.calls, "model": "fake"}


@pytest.fixture()
def sqlite_repo(tmp_path):
    from repository import SqliteBiddingRepository

    repo = SqliteBiddingRepository(str(tmp_path / "m4.db"))
    yield repo
    repo.close()
