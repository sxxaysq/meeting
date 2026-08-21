# -*- coding: utf-8 -*-
"""项目实体归一的测试。"""

from conftest import FakeClient, make_item

from models import DIFFERENT_ENTITY, ENTITY_UNCERTAIN, SAME_ENTITY, SourceItem
from project_candidate_retriever import pair_candidates, recall_signals
from project_entity_judge import ProjectEntityJudge
from project_normalizer import ProjectCatalog


def _sources(names):
    return [
        SourceItem(index=i, raw=make_item(project=name, start=i * 50, title="工作" + str(i)))
        for i, name in enumerate(names)
    ]


def test_ordinal_conflict_never_reaches_candidates():
    names = ["红沙泉一矿项目", "红沙泉二矿项目"]
    assert pair_candidates(names) == []


def test_alias_pair_is_recalled():
    signals = recall_signals("互联网收敛项目", "集团互联网收敛项目")
    assert signals is not None
    assert signals["rule"] == "containment"


def test_same_entity_merges_and_persists_alias():
    client = FakeClient(
        default={
            "decision": "SAME_ENTITY",
            "canonical_name": "集团互联网收敛项目",
            "reason": "简称与全称",
        }
    )
    catalog = ProjectCatalog()
    judge = ProjectEntityJudge(client=client, catalog=catalog)
    entities, mapping = judge.resolve(_sources(["互联网收敛项目", "集团互联网收敛项目"]))

    assert len(entities) == 1
    entity = next(iter(entities.values()))
    assert entity.canonical_name == "集团互联网收敛项目"
    assert len(set(mapping.values())) == 1
    # alias 已落库，下次会议可直接精确命中
    assert catalog.lookup("互联网收敛项目")["canonical_name"] == "集团互联网收敛项目"


def test_ordinal_pair_stays_two_entities_without_asking_model():
    client = FakeClient(default={"decision": "SAME_ENTITY", "canonical_name": "红沙泉二矿项目"})
    judge = ProjectEntityJudge(client=client, catalog=ProjectCatalog())
    entities, _ = judge.resolve(_sources(["红沙泉一矿项目", "红沙泉二矿项目"]))
    assert len(entities) == 2
    assert client.calls == []  # 根本没问模型


def test_fabricated_canonical_name_is_rejected():
    """模型自造名字（既不是 A 也不是 B）必须降级为 UNCERTAIN。"""
    client = FakeClient(
        default={"decision": "SAME_ENTITY", "canonical_name": "集团综合收敛项目"}
    )
    judge = ProjectEntityJudge(client=client, catalog=ProjectCatalog())
    decision, canonical, reason = judge.judge_pair(
        {"project_name": "互联网收敛项目"}, {"project_name": "集团互联网收敛项目"}
    )
    assert decision == ENTITY_UNCERTAIN
    assert canonical is None


def test_bare_abbreviation_is_normalised_to_full_name():
    """需求第十节：M2 应把 `红沙泉项目` 规范成 `红沙泉二矿项目`。"""
    client = FakeClient(
        default={
            "decision": "SAME_ENTITY",
            "canonical_name": "红沙泉二矿项目",
            "reason": "简称",
        }
    )
    judge = ProjectEntityJudge(client=client, catalog=ProjectCatalog())
    entities, _ = judge.resolve(_sources(["红沙泉项目", "红沙泉二矿项目"]))
    assert len(entities) == 1
    assert next(iter(entities.values())).canonical_name == "红沙泉二矿项目"


def test_abbreviation_matching_two_ordinals_becomes_uncertain():
    """简称同时匹配一矿和二矿 → 不许赌，必须 UNCERTAIN。"""
    client = FakeClient(
        default={"decision": "SAME_ENTITY", "canonical_name": "红沙泉二矿项目"}
    )
    judge = ProjectEntityJudge(client=client, catalog=ProjectCatalog())
    entities, _ = judge.resolve(
        _sources(["红沙泉项目", "红沙泉一矿项目", "红沙泉二矿项目"])
    )
    assert len(entities) == 3, "三个名字必须各自独立，不许自动归一"
    assert judge.uncertain


def test_different_entity_keeps_two_projects():
    client = FakeClient(default={"decision": "DIFFERENT_ENTITY", "canonical_name": None})
    judge = ProjectEntityJudge(client=client, catalog=ProjectCatalog())
    entities, _ = judge.resolve(_sources(["数据中台项目", "数据中台二期"]))
    assert len(entities) == 2


def test_uncertain_goes_to_review_not_alias():
    client = FakeClient(default={"decision": "UNCERTAIN", "canonical_name": None})
    catalog = ProjectCatalog()
    judge = ProjectEntityJudge(client=client, catalog=catalog)
    judge.resolve(_sources(["智慧档案室建设项目", "宁煤智慧档案室建设项目"]))
    assert judge.uncertain, "UNCERTAIN 必须被记录下来进 REVIEW"
    # 两个不同实体各自入库，但没有互相成为别名
    assert catalog.lookup("智慧档案室建设项目")["entity_id"] != catalog.lookup(
        "宁煤智慧档案室建设项目"
    )["entity_id"]


def test_no_client_yields_uncertain_not_merge():
    judge = ProjectEntityJudge(client=None, catalog=ProjectCatalog())
    decision, canonical, _ = judge.judge_pair(
        {"project_name": "互联网收敛项目"}, {"project_name": "集团互联网收敛项目"}
    )
    assert decision == ENTITY_UNCERTAIN and canonical is None
