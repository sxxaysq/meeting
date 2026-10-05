import json
from dataclasses import replace
import pytest
from src.source_admission import SourceAdmission, validated_parent, parent_entity_id
from src.models import TechnicalFailure
from .helpers import (repository, context, item, historical_task, HashEmbedder,
                      family_lookup)
from .test_service import service
from .test_revision import proposal
from src.candidate_retriever import project_compatible
from M3_KnowledgeGraph.src.task_retrieval import configure_semantics, reset_semantics


@pytest.fixture(autouse=True)
def _isolate_semantics():
    reset_semantics()
    yield
    reset_semantics()


def test_parent_grounding_without_qualifier_veto():
    """The model must point at real source text; it may now drop a mine number.

    ``validated_parent`` used to reject any proposal whose qualifier set was not a
    superset of the original's, so ``新河三矿项目-数据中心`` → ``新河`` was refused.
    Collapsing that is the intended outcome since 2026-09-17. What still has to
    hold is grounding: the span must literally occur in the source, must not be a
    bare generic noun, and must not pick one of several并列 projects.
    """
    raw = item(project='新河三矿项目-数据中心')
    assert validated_parent({'parent_span': '新河三矿', 'reason': '矿区专名主体'}, [raw])[0] == '新河三矿项目'
    # Dropping the mine number is now accepted, not rejected.
    assert validated_parent({'parent_span': '新河', 'reason': '同族统一'}, [raw])[0] == '新河项目'
    assert validated_parent({'parent_span': None, 'reason': '没有具体项目'}, [raw]) == (None, '没有具体项目')
    # In-source generic nouns collapse to 保留独立; a generic noun the model did
    # not ground in the source is a hallucination, and grounding wins (the check
    # above fires before the generic list is consulted).
    for generic in ('项目', '数据中心'):
        parent, reason = validated_parent({'parent_span': generic, 'reason': '泛称'}, [raw])
        assert parent == raw['project'] and reason.startswith('保留独立')
    for ungrounded in ('煤矿', '集团', '公司', '平台'):
        with pytest.raises(ValueError):
            validated_parent({'parent_span': ungrounded, 'reason': '不在原文'}, [raw])
    with pytest.raises(ValueError):
        validated_parent({'parent_span': '不存在的项目', 'reason': '虚构'}, [raw])
    with pytest.raises(ValueError):
        validated_parent({'parent_span': 'x', 'reason': '过短'}, [raw])
    multi = item(project='新河、蓝川项目')
    parent, reason = validated_parent({'parent_span': '新河', 'reason': '仅选一个'}, [multi])
    assert parent == multi['project'] and reason.startswith('保留独立')


def test_parent_entity_id_is_family_derived():
    assert parent_entity_id('FAM-HSQ', '红沙泉项目') == 'FAMILY-FAM-HSQ'
    # Two surfaces in one family share an identity; a different family does not.
    assert parent_entity_id('FAM-HSQ', '红沙泉项目') == parent_entity_id('FAM-HSQ', '红沙泉二矿项目')
    assert parent_entity_id('FAM-HSQ', '红沙泉项目') != parent_entity_id('FAM-THT', '陶忽图项目')
    assert parent_entity_id(None, '红沙泉项目').startswith('PARENT-')
    assert parent_entity_id(None, None) is None


def add_admission(admission, source, parent, route='M6', **extra):
    admission.rows[(source.source_document_id, source.source_item_id)] = {
        'source_item_id': source.source_item_id, 'item_index': source.item_index,
        'original_item': source.item, 'parent_project': parent, 'parent_reason': '明确父子关系',
        'route': route, 'bidding': {'category': 'TENDER', 'reason': '明确采购评审'}, **extra}


def test_parent_groups_keep_separate_goals_and_collaboration(tmp_path):
    repo = repository(tmp_path/'t.db')
    admission = SourceAdmission(repo, None)
    a = context(current_item=item(project='新河三矿项目-数据中心', title='数据中心装修', content='推进机房装修。'))
    b = replace(a, source_item_id='b', item=item(project='新河三矿智能化建设项目', title='消防系统调试', content='消防系统调试。'))
    for s in [a, b]:
        add_admission(admission, s, '新河项目', family_key='FAM-XINHE')
    workflow = service(repo, [proposal('CREATE', None), proposal('CREATE', None)])
    workflow.admission = admission
    one = workflow.process_context(a)
    two = workflow.process_context(b)
    # One family, two goals: collapsing the mine number must not collapse the tasks.
    assert one['execution']['task_id'] != two['execution']['task_id']
    assert {t['project'] for t in repo.list_tasks()} == {'新河项目'}
    assert {t['title'] for t in repo.list_tasks()} == {'数据中心装修', '消防系统调试'}
    assert workflow.process_context(a)['idempotent_replay']
    with repo.connect() as c:
        p = json.loads(c.execute('select provenance_json from task_audit limit 1').fetchone()[0])
        assert p['admission']['original_item']['project'] == '新河三矿项目-数据中心'


def test_same_family_is_compatible_across_mine_numbers(tmp_path):
    """新河三矿 and 新河二矿 are one project now; a different family still is not."""
    repo = repository(tmp_path/'t.db')
    source = context(current_item=item(project='新河三矿项目-数据中心'))
    admission = SourceAdmission(repo, None)
    add_admission(admission, source, '新河项目', family_key='FAM-XINHE')
    admitted, _ = admission.apply(source)
    assert admitted.project_entity_id == 'FAMILY-FAM-XINHE'
    configure_semantics(HashEmbedder(), family_lookup=family_lookup({
        # The admitted canonical surface must be resolvable too: with an old-scheme
        # candidate id, compatibility falls through to family_of(name) on both sides.
        '新河项目': 'FAM-XINHE',
        '新河三矿智能化建设项目': 'FAM-XINHE',
        '新河二矿项目': 'FAM-XINHE',
        '蓝川煤矿项目': 'FAM-LANCHUAN',
    }))
    assert project_compatible(admitted, {'project_entity_id': 'OLD-M2-ID', 'project': '新河三矿智能化建设项目'})
    assert project_compatible(admitted, {'project_entity_id': 'OLD-M2-ID', 'project': '新河二矿项目'})
    assert not project_compatible(admitted, {'project_entity_id': 'OLD-M2-ID', 'project': '蓝川煤矿项目'})


def test_unknown_parent_never_erases_existing_identity(tmp_path):
    repo = repository(tmp_path/'t.db')
    source = context(current_item=item(project='新客户沟通事项'))
    admission = SourceAdmission(repo, None)
    add_admission(admission, source, None)
    admitted, _ = admission.apply(source)
    assert admitted.item['project'] == source.item['project']
    assert admitted.project_entity_id == source.project_entity_id
    assert not project_compatible(admitted, {'project_entity_id': None, 'project': None})


def test_unexecuted_parent_proposals_never_become_memory(tmp_path):
    repo = repository(tmp_path/'t.db')
    admission = SourceAdmission(repo, None)
    row = {'route': 'M6', 'original_item': {'project': '新河建设项目'}, 'original_project_entity_id': 'P7',
           'parent_project': '新河项目', 'scope_validated': True}
    for day, parent in [('2026-04-07', '新河项目'), ('2026-05-01', '未来项目')]:
        (admission.directory/(day+'.json')).write_text(
            json.dumps({'source_document_id': day, 'items': {'a': {**row, 'parent_project': parent}}}))
    # Only committed audits count, and only from strictly earlier meetings.
    assert admission.known_parents('E2E-FULL-2026-04-13') == {}
    assert admission.known_parents('E2E-FULL-2026-04-01') == {}
    assert admission.known_parents('没有可靠日期') == {}


def test_known_identity_does_not_ask_model_to_rename_it(tmp_path):
    repo = repository(tmp_path/'t.db')

    class Client:
        calls = 0

        def decide(self, *_args, **_kwargs):
            self.calls += 1
            assert self.calls == 1  # Only M4 classification, no redundant parent call.
            return {'results': [{'idx': 0, 'is_bidding': False, 'category': 'NON_BIDDING', 'reason': '实施工作'}]}, {}

    client = Client()
    admission = SourceAdmission(repo, client)
    source = replace(context(current_item=item(project='星海项目')), source_document_id='2026-04-13')
    admission.memory.prior_memories = lambda _doc: [{'confirmed_parent': True, 'observed_name': '星海项目',
        'source_project_id': source.project_entity_id, 'parent_project': '星海项目'}]
    admission.memory.retrieve_names = lambda *_args: []
    admission.prepare([source])
    admitted, _ = admission.apply(source)
    assert admitted.item['project'] == '星海项目' and client.calls == 1


def test_m4_routing_is_audited_without_lifecycle_task(tmp_path):
    repo = repository(tmp_path/'t.db')
    source = context(current_item=item(title='人员定位采购评审'))
    admission = SourceAdmission(repo, None)
    add_admission(admission, source, '红沙泉项目', route='M4')
    workflow = service(repo, [])
    workflow.admission = admission
    result = workflow.process_context(source)
    assert result['execution']['action'] == 'ROUTE_M4'
    assert repo.list_tasks() == [] and repo.list_reviews() == [] and repo.list_dispatches() == []
    assert workflow.process_context(source)['idempotent_replay']
    with repo.connect() as c:
        assert c.execute('select action from task_audit').fetchone()[0] == 'ROUTE_M4'


def test_m6_project_assignment_commits_with_task_and_preserves_source_scope(tmp_path):
    repo = repository(tmp_path/'t.db')
    repo.add_historical_task(historical_task(project='新河三矿项目-数据中心', project_entity_id='OLD-ID',
                                             title='数据中心施工'))
    source = replace(context(current_item=item(project='新河三矿项目', title='数据中心施工',
                                               content='数据中心设备安装继续推进。')),
                     source_document_id='2026-04-13')
    admission = SourceAdmission(repo, None)
    admission.memory = type(admission.memory)(repo, HashEmbedder())
    add_admission(admission, source, '新河项目', family_key='FAM-XINHE')
    admission.rows[(source.source_document_id, source.source_item_id)]['scope_validated'] = True
    # The candidate task carries an old-scheme id, so retrieval compatibility is
    # decided by family_of(name): the lookup must cover both surfaces, exactly
    # like the memory graph does in production.
    configure_semantics(HashEmbedder(), family_lookup=family_lookup({
        '新河项目': 'FAM-XINHE',
        '新河三矿项目': 'FAM-XINHE',
        '新河三矿项目-数据中心': 'FAM-XINHE',
    }))
    workflow = service(repo, [proposal('PROGRESS_UPDATE', 0)])
    workflow.admission = admission
    assert workflow.process_context(source)['execution']['action'] == 'PROGRESS_UPDATE'
    task = repo.get_task('TASK-001')
    assert task['project'] == '新河项目' and task['version'] == 2
    assert task['source_project'] == '新河三矿项目-数据中心'
    memories = admission.memory.retrieve_names('新河三矿项目', '2026-04-20')
    assert memories and memories[0]['confirmed_parent'] and memories[0]['task_id'] == 'TASK-001'
    assert memories[0]['version'] == 2


def test_admission_outage_stays_technical(tmp_path):
    repo = repository(tmp_path/'t.db')

    class Failed:
        def apply(self, source):
            raise TechnicalFailure('MODEL_SERVICE: offline')

    workflow = service(repo, [])
    workflow.admission = Failed()
    assert workflow.process_context(context())['execution']['action'] == 'TECHNICAL_FAILURE'
    assert repo.list_reviews() == []
    assert repo.get_processing_record(context().source_document_id, context().source_item_id) is None
