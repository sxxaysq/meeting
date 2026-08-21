# -*- coding: utf-8 -*-
"""评估口径的测试。

重点保住一件事：``project_consistent`` 视图必须排除跨项目的 gold 合并组，
否则 M2 会被"优化"成 over-merge。
"""

from conftest import make_item

from evaluate_m2 import prf
from gold_merge_groups import (
    assign_to_gold,
    gold_pairs,
    hard_negative_pairs,
    merged_pairs_from_trace,
    project_homogeneous_gold,
    split_documents,
)


def _gold(start, end, project=None, **kwargs):
    return make_item(start=start, end=end, project=project, text="x" * (end - start), **kwargs)


def test_split_documents_uses_start_char_rollback():
    items = [_gold(0, 10), _gold(20, 30), _gold(5, 15)]
    assert [len(doc) for doc in split_documents(items)] == [2, 1]


def test_assignment_maps_predictions_into_gold_span():
    gold = [_gold(0, 100), _gold(100, 200)]
    predicted = [_gold(0, 40), _gold(40, 90), _gold(110, 190)]
    assert assign_to_gold(predicted, gold) == {0: 0, 1: 0, 2: 1}


def test_gold_pairs_from_shared_gold_item():
    assignment = {0: 0, 1: 0, 2: 1}
    assert gold_pairs(assignment) == {(0, 1)}


def test_project_consistent_view_excludes_cross_project_group():
    """标注里 170/214 的合并组跨项目——这类必须被主指标排除。"""
    gold = [_gold(0, 200, project=None)]
    strict = [
        _gold(0, 90, project="CRM二期项目"),
        _gold(100, 200, project="数据中台项目"),
    ]
    assert project_homogeneous_gold(gold, strict) == set()

    strict_same = [
        _gold(0, 90, project="红沙泉二矿项目"),
        _gold(100, 200, project="红沙泉二矿项目"),
    ]
    assert project_homogeneous_gold(gold, strict_same) == {0}


def test_hard_negatives_are_cross_project_predictions():
    gold = [_gold(0, 100, project="天地奔牛项目"), _gold(100, 200, project="红沙泉二矿项目")]
    predicted = [_gold(0, 90), _gold(110, 190)]
    assignment = assign_to_gold(predicted, gold)
    assert hard_negative_pairs(predicted, gold, assignment) == {(0, 1)}


def test_merged_pairs_from_trace_expands_cluster():
    trace = [{"source_indexes": [0, 1, 2]}, {"source_indexes": [3]}]
    assert merged_pairs_from_trace(trace) == {(0, 1), (0, 2), (1, 2)}


def test_prf_handles_empty_prediction():
    result = prf(set(), {(0, 1)})
    assert result["precision"] is None and result["recall"] == 0.0


def test_prf_counts_true_positive():
    result = prf({(0, 1), (2, 3)}, {(0, 1)})
    assert result["precision"] == 0.5 and result["recall"] == 1.0
