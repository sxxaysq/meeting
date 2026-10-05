"""Project-card policy: grouping never collapses independent task lifecycles."""
import copy
import json
from pathlib import Path

import pytest
from conftest import make_item
from test_pipeline import _client
from pipeline import run_pipeline
from project_entity_judge import ProjectEntityJudge
from project_normalizer import ProjectCatalog
from models import SourceItem


def test_0407_nine_subitems_form_one_card_with_all_evidence():
    source = json.loads((Path(__file__).parent / 'data' / '0407-project-card.json').read_text())
    original = copy.deepcopy(source)
    result = run_pipeline(source, 'generic', client=_client('KEEP_SEPARATE', entity={
        'decision': 'UNCERTAIN', 'reason': '简称不能确认'}))
    assert source == original
    cards = result.report()['project_cards']
    card = next(c for c in cards if c['project'] == '红沙泉二矿项目')
    assert card['item_indexes'] == list(range(9))
    assert card['source_indexes'] == list(range(9))
    assert len(cards) == 2  # 别处简称“红沙泉项目”保持独立。
    assert len(result.items) == 10
    assert result.validation['status'] == 'PASS'
    assert result.review == []
    assert [t.source_evidence[0] for t in result.merge_trace] == [x['evidence'] for x in source['items']]


def test_card_never_guesses_parent_or_crosses_department():
    rows = [make_item(project='红沙泉二矿项目'),
            make_item(project='红沙泉二矿项目-平台', department='别的部门', start=50),
            make_item(project='红沙泉一矿项目', start=100),
            make_item(project='未知项目-平台', start=150)]
    result = run_pipeline({'items': rows}, 'generic', client=_client('KEEP_SEPARATE'))
    assert len(result.report()['project_cards']) == 4


def test_no_transitive_entity_union_across_uncertain_pair(monkeypatch):
    import project_entity_judge as module
    names = ['甲项目', '甲项目平台', '甲项目系统']
    monkeypatch.setattr(module, 'pair_candidates', lambda _: [
        (names[0], names[1], {}), (names[1], names[2], {}), (names[0], names[2], {})])
    judge = ProjectEntityJudge(catalog=ProjectCatalog())
    def decide(left, right):
        a, b = left['project_name'], right['project_name']
        if {a, b} == {names[0], names[2]}:
            return 'UNCERTAIN', None, '无法判断'
        return 'SAME_ENTITY', a, '明确简称'
    monkeypatch.setattr(judge, 'judge_pair', decide)
    _, mapping = judge.resolve([SourceItem(i, make_item(project=n, start=i*50)) for i,n in enumerate(names)])
    assert mapping[names[0]] != mapping[names[2]]


@pytest.mark.parametrize('damage', ['schema', 'source', 'span'])
def test_hard_errors_rejected_before_catalog_write(damage):
    row = make_item()
    if damage == 'schema': row['extra'] = 'invalid'
    elif damage == 'source': row['evidence']['text'] = ''
    else: row['evidence']['end_char'] = row['evidence']['start_char']
    catalog = ProjectCatalog()
    with pytest.raises(ValueError, match='Schema/来源'):
        run_pipeline({'items': [row]}, 'generic', catalog=catalog)
    assert catalog.all_projects() == []


def test_existing_alias_conflict_is_not_downgraded():
    catalog = ProjectCatalog()
    entity = catalog.ensure_project('红沙泉二矿项目')
    catalog.add_alias(entity['entity_id'], '红沙泉项目')
    result = run_pipeline({'items': [make_item(project='红沙泉二矿项目'),
        make_item(project='红沙泉项目', start=50)]}, 'generic', catalog=catalog,
        client=_client('KEEP_SEPARATE', entity={'decision':'UNCERTAIN'}))
    assert result.validation['status'] == 'REVIEW'
    assert any(i['code'] == 'PROJECT_ALIAS_CONFLICT' and i['level'] == 'review'
               for i in result.validation['issues'])


def test_confirmed_alias_merge_does_not_report_raw_name_conflict():
    rows = [make_item(project='集团互联网收敛项目'),
            make_item(project='互联网收敛项目', start=50)]
    result = run_pipeline({'items': rows}, 'generic', client=_client('MERGE', entity={
        'decision':'SAME_ENTITY','canonical_name':'集团互联网收敛项目'}))
    assert len(result.items) == 1
    assert not any(i['code']=='OVER_MERGE_PROJECT' for i in result.validation['issues'])
