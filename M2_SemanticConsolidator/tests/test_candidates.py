# -*- coding: utf-8 -*-
"""block / generic 候选策略的差异测试。"""

from conftest import make_item

import block_candidate_strategy
import generic_candidate_strategy
import merge_candidate_retriever
from models import SourceItem


def _sources(raws):
    return [SourceItem(index=i, raw=raw) for i, raw in enumerate(raws)]


def _pairs(candidates):
    return {c.key for c in candidates}


def test_block_blocks_department_conflict():
    items = _sources(
        [
            make_item(department="智能矿山事业部", start=0, content="完成数据中心线管安装。"),
            make_item(department="数字化技术服务事业部", start=30, content="完成数据中心线管安装。"),
        ]
    )
    assert block_candidate_strategy.generate(items, {0: "P1", 1: "P1"}) == []


def test_block_blocks_delivery_group_conflict():
    items = _sources(
        [
            make_item(delivery_group="新疆交付组", start=0),
            make_item(delivery_group="陕西交付组", start=30),
        ]
    )
    assert block_candidate_strategy.generate(items, {0: "P1", 1: "P1"}) == []


def test_block_blocks_different_project_entity():
    items = _sources([make_item(start=0), make_item(start=30)])
    assert block_candidate_strategy.generate(items, {0: "P1", 1: "P2"}) == []


def test_block_recalls_adjacent_same_project():
    items = _sources(
        [
            make_item(start=0, title="数据中心线管", content="完成顶面线管安装100%。"),
            make_item(start=40, title="数据中心空调", content="确保空调设备100%到货。"),
        ]
    )
    assert _pairs(block_candidate_strategy.generate(items, {0: "P1", 1: "P1"})) == {(0, 1)}


def test_block_uses_project_section_context_for_null_projects():
    """两条自己都没写项目名，但分属不同项目段落 → 不进候选。
    实测 05-11 的两条"跟进商机"就是这样被错合的。"""
    items = _sources(
        [
            make_item(start=0, project="A项目", title="A推进", content="推进A项目实施。"),
            make_item(start=40, project=None, title="跟进商机", content="跟进商机推进情况。"),
            make_item(start=80, project="B项目", title="B推进", content="推进B项目实施。"),
            make_item(start=120, project=None, title="跟进商机", content="跟进商机推进情况。"),
        ]
    )
    project_of = {0: "P1", 1: None, 2: "P2", 3: None}
    pairs = _pairs(block_candidate_strategy.generate(items, project_of))
    assert (1, 3) not in pairs, "分属两个项目段落的空项目条目不该进候选"


def test_block_treats_pre_first_project_as_its_own_section():
    """部门里第一个项目名出现**之前**的条目，自成一个段落，
    不能和后面项目段落里的条目合并。实测 05-11 的错合就是这一类。"""
    items = _sources(
        [
            make_item(start=0, project=None, title="跟进商机", content="跟进电子采购平台商机进度。"),
            make_item(start=40, project="A项目", title="A推进", content="推进A项目实施。"),
            make_item(start=80, project=None, title="跟进商机", content="跟进商机推进情况。"),
        ]
    )
    pairs = _pairs(block_candidate_strategy.generate(items, {0: None, 1: "P1", 2: None}))
    assert (0, 2) not in pairs


def test_block_section_context_keeps_same_section_pair():
    """同一项目段落内的两条空项目条目仍然可以进候选。"""
    items = _sources(
        [
            make_item(start=0, project="A项目", title="A推进", content="推进A项目实施。"),
            make_item(start=40, project=None, title="跟进商机", content="跟进商机推进情况。"),
            make_item(start=80, project=None, title="跟进进度", content="跟进商机反馈进度。"),
        ]
    )
    pairs = _pairs(block_candidate_strategy.generate(items, {0: "P1", 1: None, 2: None}))
    assert (1, 2) in pairs


def test_block_section_context_is_per_department():
    """项目段落上下文按部门隔离，别的部门的项目名不该串进来。"""
    items = _sources(
        [
            make_item(start=0, department="甲部", project="A项目", title="A", content="推进A。"),
            make_item(start=40, department="乙部", project=None, title="教育活动", content="组织开展警示教育。"),
            make_item(start=80, department="甲部", project="B项目", title="B", content="推进B。"),
            make_item(start=120, department="乙部", project=None, title="教育活动策划", content="做好警示教育策划。"),
        ]
    )
    pairs = _pairs(block_candidate_strategy.generate(items, {0: "P1", 1: None, 2: "P2", 3: None}))
    assert (1, 3) in pairs, "乙部两条不该被甲部的项目边界隔开"


def test_generic_ignores_null_work_section():
    """generic 下 work_section=null 不得当成错误或冲突。"""
    items = _sources(
        [
            make_item(work_section=None, project=None, start=0, content="完成顶面线管安装。"),
            make_item(work_section=None, project=None, start=40, content="完成桥架安装。"),
        ]
    )
    assert _pairs(generic_candidate_strategy.generate(items, {0: None, 1: None})) == {(0, 1)}


def test_generic_recalls_on_shared_assignee_across_distance():
    items = _sources(
        [
            make_item(
                project=None, assignee=["王超"], start=0, title="模型测试", content="完成模型测试。"
            ),
            make_item(
                project=None,
                assignee=["王超"],
                start=1200,
                title="接口开发",
                content="推动北京院给出方案接口。",
            ),
        ]
    )
    pairs = _pairs(generic_candidate_strategy.generate(items, {0: None, 1: None}))
    assert (0, 1) in pairs


def test_generic_still_blocks_different_project_entity():
    items = _sources([make_item(start=0), make_item(start=40)])
    assert generic_candidate_strategy.generate(items, {0: "P1", 1: "P2"}) == []


def test_retriever_rejects_unknown_mode():
    try:
        merge_candidate_retriever.generate("guess", [], {})
    except ValueError as error:
        assert "source_mode" in str(error)
    else:
        raise AssertionError("未知模式必须报错，不许猜")


def test_candidate_cap_limits_explosion():
    items = _sources([make_item(start=i * 20, title="数据中心", content="推进数据中心建设。") for i in range(12)])
    candidates = merge_candidate_retriever.generate(
        "block", items, {i: "P1" for i in range(12)}
    )
    counts = {}
    for candidate in candidates:
        counts[candidate.left] = counts.get(candidate.left, 0) + 1
        counts[candidate.right] = counts.get(candidate.right, 0) + 1
    assert max(counts.values()) <= merge_candidate_retriever.MAX_CANDIDATES_PER_ITEM
