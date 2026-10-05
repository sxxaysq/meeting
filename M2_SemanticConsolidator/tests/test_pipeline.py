# -*- coding: utf-8 -*-
"""端到端链路 + Schema + 质量门。

覆盖需求里点名的两个场景：
* 同一交付物被 M1 拆成多条 → 应该合并；
* 同一项目下的不同子系统 → **绝不能**合并（需求第七节原文举例）。
"""

import json

import pytest
from conftest import FakeClient, make_item

import schema_validator
from models import ERROR, PASS, REVIEW
from pipeline import run_pipeline
from project_normalizer import ProjectCatalog


def _payload(items):
    return {"items": items}


def _client(merge_decision="MERGE", cluster_decision="KEEP_CLUSTER", entity=None):
    """按提示词特征分派：项目实体判定 / 合并判定 / 簇校验 / 标题生成。"""
    rules = [
        (lambda s, u: "项目实体归一判定器" in s, entity or {"decision": "DIFFERENT_ENTITY"}),
        (lambda s, u: "合并簇校验器" in s, {"decision": cluster_decision, "reason": "测试"}),
        (lambda s, u: "业务事项归并判定器" in s, {"decision": merge_decision, "reason": "测试"}),
        (lambda s, u: "标题生成器" in s, {"title": None}),
    ]
    return FakeClient(rules=rules, default={})


def test_split_delivery_is_merged():
    text = "完成顶面线管安装100%。\n确保空调设备100%到货。"
    items = [
        make_item(start=0, end=13, title="线管安装", content="完成顶面线管安装100%。"),
        make_item(start=14, end=len(text), title="空调到货", content="确保空调设备100%到货。"),
    ]
    result = run_pipeline(
        _payload(items), "block", client=_client("MERGE"), normalized_text=text
    )
    assert len(result.items) == 1
    assert result.items[0]["content"] == "完成顶面线管安装100%。\n确保空调设备100%到货。"
    assert result.merge_trace[0].source_indexes == [0, 1]
    assert result.merge_trace[0].merged is True


def test_same_project_different_subsystems_are_not_merged():
    """需求第七节点名的反例：同项目不同子系统必须各自保留。"""
    items = [
        make_item(start=0, end=20, title="数据中心建设", content="推进数据中心建设。"),
        make_item(start=25, end=48, title="综合管控平台研发", content="完成综合管控平台研发。"),
        make_item(start=50, end=70, title="火灾监测系统", content="完成火灾监测系统招标。"),
    ]
    result = run_pipeline(_payload(items), "block", client=_client("KEEP_SEPARATE"))
    assert len(result.items) == 3
    assert all(not entry.merged for entry in result.merge_trace)


def test_uncertain_pair_stays_separate_with_warning():
    items = [
        make_item(start=0, end=20, content="推进数据中心建设。"),
        make_item(start=25, end=48, content="完成综合管控平台研发。"),
    ]
    result = run_pipeline(_payload(items), "block", client=_client("UNCERTAIN"))
    assert len(result.items) == 2
    assert result.validation["status"] == PASS
    assert "MERGE_UNCERTAIN" in result.validation["issue_counts"]


def test_cluster_review_keeps_items_split():
    items = [
        make_item(start=i * 30, end=i * 30 + 20, content="推进数据中心建设第{}步。".format(i))
        for i in range(3)
    ]
    result = run_pipeline(
        _payload(items), "block", client=_client("MERGE", cluster_decision="REVIEW")
    )
    assert len(result.items) == 3
    assert "CLUSTER_REVIEW" in result.validation["issue_counts"]


def test_transitive_department_conflict_vetoes_merge():
    """A(部门空) 与 B(甲部门) 不冲突、A 与 C(乙部门) 也不冲突，
    但连成一簇后甲乙冲突——必须否决合并，而不是合完把 department 置空。
    实测 05-25 出现过一簇 5 条跨多个部门。"""
    items = [
        make_item(start=0, end=20, department=None, content="配合巡视工作。"),
        make_item(start=25, end=48, department="安全生产部", content="协助开展巡视相关工作。"),
        make_item(start=50, end=75, department="市场经营部", content="配合准备巡视材料。"),
    ]
    result = run_pipeline(_payload(items), "block", client=_client("MERGE"))
    assert len(result.items) == 3
    assert all(not entry.merged for entry in result.merge_trace)
    assert "STRUCTURE_CONFLICT_VETO" in result.validation["issue_counts"]
    # 部门信息不许被合并抹掉
    assert [item["department"] for item in result.items] == [
        None,
        "安全生产部",
        "市场经营部",
    ]


def test_no_conflict_still_merges():
    """空值与单一非空值共存不算冲突，仍然允许合并。"""
    items = [
        make_item(start=0, end=20, department=None, content="配合巡视工作。"),
        make_item(start=25, end=48, department="安全生产部", content="协助开展巡视相关工作。"),
    ]
    result = run_pipeline(_payload(items), "block", client=_client("MERGE"))
    assert len(result.items) == 1
    assert result.items[0]["department"] == "安全生产部"


def test_output_passes_schema():
    items = [make_item(start=0, end=20), make_item(start=30, end=50, title="另一件事", content="另一件事内容。")]
    result = run_pipeline(_payload(items), "block", client=_client("KEEP_SEPARATE"))
    payload = result.payload()
    assert schema_validator.validate(payload) == []
    # 顶层是 M2 的信息，items 仍是 M1 的九字段
    assert set(payload) == {
        "source_mode",
        "items",
        "project_entities",
        "merge_trace",
        "validation",
    }
    assert set(payload["items"][0]) == {
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


def test_schema_rejects_forbidden_fields():
    payload = {
        "source_mode": "block",
        "items": [{**make_item(), "confidence": 0.9}],
        "project_entities": [],
        "merge_trace": [
            {
                "item_index": 0,
                "source_indexes": [0],
                "merged": False,
                "evidence_mode": "single",
                "evidence_contiguous": True,
                "source_evidence": [],
            }
        ],
        "validation": {"status": "PASS", "issues": []},
    }
    errors = schema_validator.validate(payload)
    assert any("confidence" in error for error in errors)


def test_schema_requires_full_trace_coverage():
    payload = {
        "source_mode": "block",
        "items": [make_item(), make_item()],
        "project_entities": [],
        "merge_trace": [
            {
                "item_index": 0,
                "source_indexes": [0],
                "merged": False,
                "evidence_mode": "single",
                "evidence_contiguous": True,
                "source_evidence": [],
            }
        ],
        "validation": {"status": "PASS", "issues": []},
    }
    assert any("未覆盖全部 items" in error for error in schema_validator.validate(payload))


def test_generic_null_work_section_is_not_an_error():
    """需求第十二节第 7 条：generic 缺 work_section/project 不得判 ERROR。"""
    items = [
        make_item(start=0, end=20, work_section=None, project=None, content="推进数据中心建设。"),
        make_item(
            start=400, end=430, work_section=None, project=None,
            title="人力工作", content="完成人员招聘计划。",
        ),
    ]
    result = run_pipeline(_payload(items), "generic", client=_client("KEEP_SEPARATE"))
    assert result.validation["status"] != ERROR
    assert not any(
        issue["code"] == "WORK_SECTION_MOSTLY_MISSING" for issue in result.validation["issues"]
    )


def test_uncertain_projects_are_audited_without_human_review():
    """独立处理已消除合并风险，warning 保留在 validation 而非人工队列。"""
    entity = {"decision": "UNCERTAIN", "canonical_name": None, "reason": "指代不明"}
    items = [
        make_item(start=0, end=20, project="红沙泉项目", content="推进红沙泉工作。"),
        make_item(
            start=300, end=330, project="红沙泉二矿项目",
            title="另一件事", content="完成二矿验收。",
        ),
    ]
    result = run_pipeline(
        _payload(items), "block", client=_client("KEEP_SEPARATE", entity=entity)
    )
    assert result.review == []
    codes = {issue['code'] for issue in result.validation['issues']}
    assert "PROJECT_ENTITY_UNCERTAIN" in codes
    assert all(issue['level'] == 'warning' for issue in result.validation['issues'])


def test_source_mode_is_not_guessed():
    with pytest.raises(ValueError):
        run_pipeline(_payload([make_item()]), "auto", client=None)


def test_project_canonicalisation_flows_into_items():
    """M1 给 `互联网收敛项目`，M2 归一成 `集团互联网收敛项目`——这正是 M2 与 M1 的职责区别。"""
    entity = {
        "decision": "SAME_ENTITY",
        "canonical_name": "集团互联网收敛项目",
        "reason": "简称",
    }
    items = [
        make_item(start=0, end=20, project="互联网收敛项目", content="推进收敛工作。"),
        make_item(
            start=300, end=330, project="集团互联网收敛项目",
            title="另一件事", content="完成收敛验收。",
        ),
    ]
    result = run_pipeline(
        _payload(items), "block", client=_client("KEEP_SEPARATE", entity=entity)
    )
    assert {item["project"] for item in result.items} == {"集团互联网收敛项目"}
    assert len(result.project_entities) == 1
    assert result.project_entities[0]["canonical_name"] == "集团互联网收敛项目"


def test_ungrounded_project_is_reported_as_error():
    from models import MergeTraceEntry, SourceItem
    import quality_gate

    source = SourceItem(index=0, raw=make_item(project="红沙泉二矿项目"))
    item = {**make_item(project="完全不存在的项目")}
    trace = [
        MergeTraceEntry(
            item_index=0,
            source_indexes=[0],
            merged=False,
            evidence_mode="single",
            contiguous=True,
        )
    ]
    result = quality_gate.run(
        items=[item], trace=trace, source_items=[source], source_mode="block",
        schema_errors=[], uncertain_projects=[], alias_conflicts=[], alias_pool=set(),
    )
    assert result["status"] == ERROR
    assert "PROJECT_NOT_GROUNDED" in result["issue_counts"]


def test_catalog_alias_persists_across_meetings():
    catalog = ProjectCatalog()
    entity = {"decision": "SAME_ENTITY", "canonical_name": "集团互联网收敛项目", "reason": ""}
    first = [
        make_item(start=0, end=20, project="互联网收敛项目", content="推进收敛工作。"),
        make_item(start=300, end=330, project="集团互联网收敛项目", title="别的", content="完成收敛验收。"),
    ]
    run_pipeline(_payload(first), "block", client=_client("KEEP_SEPARATE", entity=entity), catalog=catalog)

    # 第二次会议只出现简称，靠已落库的 alias 直接命中，不必再问模型
    second = [make_item(start=0, end=20, project="互联网收敛项目", content="继续推进。")]
    result = run_pipeline(
        _payload(second), "block", client=_client("KEEP_SEPARATE"), catalog=catalog
    )
    assert result.items[0]["project"] == "集团互联网收敛项目"
