# -*- coding: utf-8 -*-
"""classifier.py 单元测试：LLM 校验、正则交叉校验、切分。"""

import pytest
from conftest import FakeLLMClient, make_bidding_item, make_item

from classifier import (
    ClassificationError,
    classify_items,
    cross_check,
    regex_hits,
    split_items,
)


def test_classify_basic_partition():
    items = [make_bidding_item(), make_item(), make_item(title="参与塔然高勒项目投标")]
    decisions = {0: (True, "TENDER", "编制招标文件"), 2: (True, "BID", "参与投标")}
    result = classify_items(items, FakeLLMClient(decisions))
    assert result["bidding_indices"] == [0, 2]
    assert result["annotations"][0]["category"] == "TENDER"
    assert result["annotations"][1]["is_bidding"] is False
    assert result["llm_stats"]["calls"] == 1


def test_classify_chunking_multiple_calls():
    items = [make_item() for _ in range(10)]
    client = FakeLLMClient({})
    result = classify_items(items, client, chunk_size=4)
    # 10 条、chunk=4 → 3 次调用，且 idx 全覆盖
    assert client.calls == 3
    assert sorted(result["annotations"]) == list(range(10))


@pytest.mark.parametrize(
    "raw",
    [
        {"results": [{"idx": 0, "is_bidding": True, "category": "TENDER", "reason": "x"}]},
        # 缺 idx=1
        {"results": [
            {"idx": 0, "is_bidding": False, "category": "NON_BIDDING", "reason": "x"},
            {"idx": 1, "is_bidding": False, "category": "NON_BIDDING", "reason": "x"},
            {"idx": 1, "is_bidding": False, "category": "NON_BIDDING", "reason": "x"},
        ]},
        # category 越界
        {"results": [
            {"idx": 0, "is_bidding": False, "category": "NON_BIDDING", "reason": "x"},
            {"idx": 1, "is_bidding": True, "category": "TENDERING", "reason": "x"},
        ]},
        # is_bidding=false 但 category 非 NON_BIDDING
        {"results": [
            {"idx": 0, "is_bidding": False, "category": "NON_BIDDING", "reason": "x"},
            {"idx": 1, "is_bidding": False, "category": "TENDER", "reason": "x"},
        ]},
        # reason 缺失
        {"results": [
            {"idx": 0, "is_bidding": False, "category": "NON_BIDDING", "reason": "x"},
            {"idx": 1, "is_bidding": True, "category": "BID", "reason": "  "},
        ]},
    ],
)
def test_classify_invalid_outputs_raise(raw):
    items = [make_item(), make_item()]
    with pytest.raises(ClassificationError):
        classify_items(items, FakeLLMClient({}, invalid_output=raw))


def test_classify_unparsable_output_raises():
    with pytest.raises(ClassificationError):
        # invalid_output=False 作为哨兵：fake 直接返回不可解析的假值
        classify_items([make_item()], FakeLLMClient({}, invalid_output=False))


def test_regex_hits_only():
    assert regex_hits(make_bidding_item()) is True
    assert regex_hits(make_item()) is False
    # 履约/到货等一般工作即使含"项目"也不算强命中
    assert regex_hits(make_item(content="推进项目验收与付款")) is False


def test_cross_check_records_but_not_flips():
    items = [make_bidding_item(), make_item()]
    annotations = {
        0: {"idx": 0, "is_bidding": False, "category": "NON_BIDDING", "reason": "误判示例"},
        1: {"idx": 1, "is_bidding": True, "category": "BID", "reason": "无关键词命中示例"},
    }
    disagreements = cross_check(items, annotations)
    kinds = {d["kind"] for d in disagreements}
    assert "regex_hit_llm_no" in kinds and "llm_yes_no_keyword" in kinds
    # 不改判：annotations 保持原样
    assert annotations[0]["is_bidding"] is False


def test_split_items_partition_and_fields():
    items = [make_bidding_item(), make_item()]
    annotations = {
        0: {"idx": 0, "is_bidding": True, "category": "TENDER", "reason": "招标文件编制"},
        1: {"idx": 1, "is_bidding": False, "category": "NON_BIDDING", "reason": "实施工作"},
    }
    split = split_items(items, annotations)
    assert len(split["bidding"]) == 1 and len(split["filtered"]) == 1
    entry = split["bidding"][0]
    assert entry["idx"] == 0
    assert entry["bidding_category"] == "TENDER"
    # filtered 保持九字段原样（不含注记字段）
    filtered_item = split["filtered"][0]
    assert set(filtered_item.keys()) == {
        "department", "work_section", "delivery_group", "project", "item_type",
        "assignee", "title", "content", "evidence",
    }
