# -*- coding: utf-8 -*-
"""确定性文本工具的测试，重点是序数硬否决。"""

import pytest

from text_utils import (
    ambiguous_ordinal,
    containment,
    core_name,
    grounding_ratio,
    normalize_name,
    ordinal_conflict,
    ordinal_tokens,
)


def test_normalize_strips_punctuation_and_width():
    assert normalize_name("红沙泉二矿  项目") == normalize_name("红沙泉二矿项目")
    assert normalize_name("ＣＲＭ二期项目") == normalize_name("CRM二期项目")


def test_core_name_strips_generic_suffix():
    assert core_name("集团互联网收敛项目") == core_name("集团互联网收敛")
    # 剥到只剩通用词时不再继续剥，避免把名字剥没
    assert core_name("项目") == "项目"


@pytest.mark.parametrize(
    "left,right",
    [
        ("红沙泉一矿项目", "红沙泉二矿项目"),
        ("CRM二期项目", "CRM三期项目"),
        ("2025 年网络安全防护项目", "2026 年网络安全防护项目"),
    ],
)
def test_ordinal_conflict_blocks_dangerous_pairs(left, right):
    """需求第六节点名的高危对：两边都带序数且不同，字面极近但一定是不同项目。"""
    assert ordinal_conflict(left, right) is True


@pytest.mark.parametrize(
    "left,right",
    [
        ("集团总部信息云资源池扩容项目", "信息云资源池扩容项目"),
        ("互联网收敛项目", "集团互联网收敛项目"),
        ("集团总部2025 年网络安全防护项目", "2025 年网络安全防护项目"),
        ("沈阳院MES", "沈阳院MES 项目"),
        # 需求第十节点名要归一的情形：一边没有序数时不硬否决，交给 LLM
        ("红沙泉项目", "红沙泉二矿项目"),
    ],
)
def test_ordinal_conflict_allows_real_aliases(left, right):
    """真实别名对不应被序数规则误伤。"""
    assert ordinal_conflict(left, right) is False


def test_ambiguous_ordinal_guards_bare_abbreviation():
    """简称同时匹配一矿和二矿时无法确定归属，必须挡住。"""
    assert ambiguous_ordinal("红沙泉项目", ["红沙泉一矿项目", "红沙泉二矿项目"]) is True
    assert ambiguous_ordinal("红沙泉项目", ["红沙泉二矿项目"]) is False
    # 自己带序数的名字不受这条规则约束
    assert ambiguous_ordinal("红沙泉二矿项目", ["红沙泉一矿项目", "红沙泉项目"]) is False


def test_ordinal_tokens_normalises_cjk_and_arabic():
    assert ordinal_tokens("二期") == ordinal_tokens("2 期")


def test_containment():
    assert containment("互联网收敛项目", "集团互联网收敛项目") is True
    assert containment("天地奔牛项目", "红沙泉二矿项目") is False


def test_grounding_ratio_detects_invented_content():
    sources = ["完成顶面线管、桥架安装100%。", "确保空调设备100%到货。"]
    # 合法概括跨过原文标点会丢一个 bigram（"管安"），仍应高于阈值
    assert grounding_ratio("顶面线管安装", sources) >= 0.75
    assert grounding_ratio("全面推进智慧矿山战略转型", sources) < 0.5
