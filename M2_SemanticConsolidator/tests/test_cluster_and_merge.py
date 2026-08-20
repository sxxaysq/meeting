# -*- coding: utf-8 -*-
"""聚簇、传递式错合切断、合并字段生成、evidence 规则。"""

from conftest import FakeClient, make_item

import merger
from cluster_validator import ClusterValidator, build_clusters
from merge_judge import MergeJudge
from models import (
    CLUSTER_REVIEW,
    KEEP_CLUSTER,
    MERGE,
    MERGE_UNCERTAIN,
    Cluster,
    MergeCandidate,
    PairJudgement,
    SourceItem,
)


def _sources(raws):
    return [SourceItem(index=i, raw=raw) for i, raw in enumerate(raws)]


def test_union_find_builds_transitive_cluster():
    items = _sources([make_item(start=i * 30) for i in range(3)])
    judgements = [
        PairJudgement(0, 1, MERGE),
        PairJudgement(1, 2, MERGE),
    ]
    clusters = build_clusters(judgements, items)
    assert [c.members for c in clusters] == [[0, 1, 2]]


def test_cluster_validator_splits_transitive_error():
    """A≈B、B≈C 但 A 与 C 无关 → 必须拆开，这是需求第九节的核心。"""
    items = _sources([make_item(start=i * 30) for i in range(3)])
    client = FakeClient(
        default={"decision": "SPLIT_CLUSTER", "groups": [[0, 1], [2]], "reason": "2 是另一件事"}
    )
    validator = ClusterValidator(client=client)
    result = validator.validate([Cluster(members=[0, 1, 2])], items)
    assert [c.members for c in result] == [[0, 1], [2]]


def test_invalid_split_groups_degrade_to_review():
    """模型给的分组漏了成员 → 不猜它想说什么，整簇转人工。"""
    items = _sources([make_item(start=i * 30) for i in range(3)])
    client = FakeClient(default={"decision": "SPLIT_CLUSTER", "groups": [[0, 1]]})
    result = ClusterValidator(client=client).validate([Cluster(members=[0, 1, 2])], items)
    assert len(result) == 1 and result[0].decision == CLUSTER_REVIEW


def test_oversized_cluster_forced_to_review():
    items = _sources([make_item(start=i * 30) for i in range(8)])
    client = FakeClient(default={"decision": "KEEP_CLUSTER", "reason": "都一样"})
    result = ClusterValidator(client=client).validate(
        [Cluster(members=list(range(8)))], items
    )
    assert result[0].decision == CLUSTER_REVIEW


def test_missing_client_makes_cluster_review_not_merge():
    items = _sources([make_item(start=i * 30) for i in range(3)])
    result = ClusterValidator(client=None).validate([Cluster(members=[0, 1, 2])], items)
    assert result[0].decision == CLUSTER_REVIEW


def test_merge_judge_hides_recall_signals_from_model():
    """召回信号不能出现在给模型的输入里，否则会诱导模型往合并方向靠。"""
    items = _sources([make_item(start=0), make_item(start=40)])
    client = FakeClient(default={"decision": "KEEP_SEPARATE", "reason": "两件事"})
    judge = MergeJudge(client=client)
    judge.judge(
        [MergeCandidate(0, 1, {"gap": 40, "similarity": 0.9, "same_project_entity": True})],
        items,
    )
    _system, user = client.calls[0]
    for leaked in ("similarity", "gap", "same_project_entity", "0.9"):
        assert leaked not in user


def test_unknown_decision_becomes_uncertain():
    items = _sources([make_item(start=0), make_item(start=40)])
    client = FakeClient(default={"decision": "PROBABLY", "reason": ""})
    judge = MergeJudge(client=client)
    result = judge.judge([MergeCandidate(0, 1)], items)
    assert result[0].decision == MERGE_UNCERTAIN


# ---- 合并字段规则 -------------------------------------------------


def test_content_is_concatenation_only():
    sources = _sources(
        [
            make_item(start=0, content="完成顶面线管安装100%。"),
            make_item(start=30, content="确保空调设备100%到货。"),
        ]
    )
    content = merger.merge_content(sources)
    assert content == "完成顶面线管安装100%。\n确保空调设备100%到货。"


def test_content_dedupes_identical_sources():
    sources = _sources(
        [make_item(start=0, content="持续推进。"), make_item(start=30, content="持续推进。")]
    )
    assert merger.merge_content(sources) == "持续推进。"


def test_assignee_is_union_without_invention():
    sources = _sources(
        [
            make_item(start=0, assignee=["王超"]),
            make_item(start=30, assignee=["王超", "尤梦雅"]),
        ]
    )
    assert merger.merge_assignee(sources) == ["王超", "尤梦雅"]


def test_structural_conflict_returns_none_for_review():
    sources = _sources(
        [make_item(start=0, delivery_group="新疆交付组"), make_item(start=30, delivery_group="陕西交付组")]
    )
    value, conflict = merger.merge_structural_field(sources, "delivery_group", generic=False)
    assert value is None and "冲突" in conflict


def test_null_and_value_merge_takes_the_value():
    sources = _sources(
        [make_item(start=0, work_section=None), make_item(start=30, work_section="项目推进")]
    )
    value, conflict = merger.merge_structural_field(sources, "work_section", generic=True)
    assert value == "项目推进" and conflict is None


def test_ungrounded_llm_title_falls_back_to_source():
    sources = _sources([make_item(start=0, title="数据中心线管安装")])
    client = FakeClient(default={"title": "全面推进智慧矿山战略转型升级工程"})
    title, origin = merger.generate_title(sources, "完成顶面线管安装100%。", client)
    assert origin == "source_grounding_failed"
    assert title == "数据中心线管安装"


def test_grounded_llm_title_is_accepted():
    sources = _sources([make_item(start=0, title="线管安装")])
    client = FakeClient(default={"title": "顶面线管安装"})
    title, origin = merger.generate_title(sources, "完成顶面线管安装100%。", client)
    assert origin == "llm" and title == "顶面线管安装"


# ---- evidence -----------------------------------------------------


def test_bounding_evidence_is_literal_slice():
    text = "完成顶面线管安装100%。\n确保空调设备100%到货。"
    first = text.index("完成")
    second = text.index("确保")
    sources = _sources(
        [
            make_item(start=first, end=first + 13, content="完成顶面线管安装100%。"),
            make_item(start=second, end=len(text), content="确保空调设备100%到货。"),
        ]
    )
    evidence, mode, contiguous = merger.merge_evidence(sources, text)
    assert mode == "bounding_span"
    assert evidence["text"] == text[evidence["start_char"] : evidence["end_char"]]
    assert evidence["exact_match"] is True
    assert contiguous is True


def test_non_contiguous_evidence_is_flagged():
    text = "完成顶面线管安装。\n另一个部门的无关事项。\n确保空调设备到货。"
    sources = _sources(
        [
            make_item(start=0, end=9, content="完成顶面线管安装。"),
            make_item(start=text.index("确保"), end=len(text), content="确保空调设备到货。"),
        ]
    )
    _evidence, _mode, contiguous = merger.merge_evidence(sources, text)
    assert contiguous is False


def test_without_normalized_text_no_fabricated_exact_match():
    """没有规范化文本时不许拼字符串充当 evidence。"""
    sources = _sources(
        [
            make_item(start=0, end=10, content="A", exact_match=True),
            make_item(start=50, end=60, content="B", exact_match=True),
        ]
    )
    evidence, mode, _ = merger.merge_evidence(sources, None)
    assert mode == "primary_source"
    assert evidence == sources[0].raw["evidence"]
