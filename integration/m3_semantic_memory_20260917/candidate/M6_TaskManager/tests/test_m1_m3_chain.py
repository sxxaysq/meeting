import json
from dataclasses import replace
import pytest
from M6_TaskManager.src.m1_input import load_m1_payload
from M6_TaskManager.src.models import InputContractError, SourceContext
from M6_TaskManager.src.repository import TaskRepository
from M3_KnowledgeGraph.src.project_memory import ProjectMemory
from M3_KnowledgeGraph.src.task_retrieval import (
    project_compatible, configure_semantics, reset_semantics,
)
from M6_TaskManager.src.source_admission import SourceAdmission
from src.candidate_retriever import project_compatible as m6_project_compatible
from .helpers import HashEmbedder, family_lookup


@pytest.fixture(autouse=True)
def _isolate_semantics():
    reset_semantics()
    yield
    reset_semantics()


def native_item(project='新河二矿项目-数据中心'):
    return {'department':'研发部','work_section':None,'delivery_group':None,'project':project,
        'item_type':'PROJECT_TASK','assignee':[],'title':'数据中心施工','content':'推进数据中心施工。',
        'evidence':{'text':'推进数据中心施工。','page_start':1,'page_end':1,'start_char':0,'end_char':10,'exact_match':True}}


def test_native_m1_one_to_one_without_m2(tmp_path,monkeypatch):
    from M6_TaskManager.src import m2_input
    monkeypatch.setattr(m2_input,'load_m2_payload',lambda *_a,**_k:pytest.fail('M2 must not run'))
    p=tmp_path/'m1.json';original={'items':[native_item(),native_item('新河二矿项目-集控中心')],'mode':'generic','source_document_id':'原始会议.pdf'}
    p.write_text(json.dumps(original))
    doc,contexts=load_m1_payload(p,document_id='E2E-FULL-2026-04-07')
    assert [c.item for c in contexts]==original['items']
    assert [c.merge_trace['source_indexes'] for c in contexts]==[[0],[1]]
    assert all(not c.merge_trace['merged'] and c.input_stage=='M1' for c in contexts)
    assert all('m2_validation' not in c.provenance for c in contexts)
    assert all(c.provenance['origin_document_id']=='原始会议.pdf' for c in contexts)
    original['items'][0]['task_id']='model-injected'
    p.write_text(json.dumps(original))
    with pytest.raises(InputContractError):load_m1_payload(p,document_id=doc)


def test_m1_hard_source_error_and_m2_envelope_rejected(tmp_path):
    p=tmp_path/'bad.json'
    for payload in [{'items':[native_item()],'merge_trace':[]},
                    {'items':[{**native_item(),'evidence':{**native_item()['evidence'],'text':' '}}]}]:
        p.write_text(json.dumps(payload))
        with pytest.raises(InputContractError):load_m1_payload(p,document_id='2026-04-07')


def test_rag_similar_name_is_source_grounded_and_date_bounded(tmp_path):
    repo=TaskRepository(tmp_path/'t.db');repo.initialize()
    repo.upsert_department('D','研发部','test://never-send')
    memory=ProjectMemory(repo,HashEmbedder())
    row={'item_index':0,'route':'M6','source_item_id':'S1','source_project_id':'RAW-OLD',
        'original_item':native_item('新河二矿项目'),'parent_project':'新河二矿项目'}
    for day,parent in [('2026-04-07','新河二矿项目'),('2026-08-01','新河二矿未来项目')]:
        task_id='T-'+day
        repo.add_historical_task({'task_id':task_id,'item_type':'PROJECT_TASK','department_id':'D','department':'研发部',
            'project':parent,'project_entity_id':'PARENT-'+day,'title':'数据中心施工','description':'施工','status':'OPEN'})
        with repo.connect() as c:
            c.execute('''INSERT INTO task_audit(audit_id,task_id,action,reason,source_document_id,source_item_id,provenance_json,created_at)
                VALUES (?,?,?,?,?,?,?,?)''',('A-'+day,task_id,'CREATE','fixture',day,'S1',json.dumps({'admission':{**row,'scope_validated':True}}),day))
            c.commit()
    hits=memory.retrieve_names('新河二矿智能化建设服务项目','2026-04-13')
    # Date bounding is unchanged: the 2026-08-01 audit is in the future, so only
    # the 2026-04-07 one may be recalled.
    assert hits and all(h['source_document_id']=='2026-04-07' for h in hits)
    current=native_item('某能源集团新河二矿智能化建设服务项目')
    answer={'parent_span':'新河二矿','parent_index':0,'reason':'原文明确为同一二矿整体建设'}
    assert SourceAdmission.resolve_reference(answer,[current],hits)[0]=='新河二矿项目'
    # 2026-09-17: a different mine number in the same family is no longer a veto.
    # 新河一矿 and 新河二矿 resolve to one family, so the recalled parent is reused.
    sibling=native_item('新河一矿项目')
    assert SourceAdmission.resolve_reference({**answer,'parent_span':'新河一矿'},[sibling],hits)[0]=='新河二矿项目'
    # A different proper noun is still refused, by resolved family rather than by
    # comparing qualifier strings.
    unrelated=native_item('安标中心项目')
    ambiguous=[{**hits[0],'parent_project':'安标国家中心项目','observed_name':'安标国家中心项目'}]
    lookup=family_lookup({'安标中心项目':'FAM-ANBIAO','安标国家中心项目':'FAM-ANBIAO-GUOJIA'})
    parent,reason=SourceAdmission.resolve_reference(
        {'parent_span':'安标中心','parent_index':0,'reason':'名称相似'},[unrelated],ambiguous,lookup)
    assert parent=='安标中心项目' and '保持独立' in reason
    assert hits[0]['task_id']=='T-2026-04-07' and hits[0]['version']==1 and hits[0]['status']=='OPEN'
    assert not hasattr(memory,'client') and not hasattr(memory,'prepare')


def test_sibling_subsystems_share_one_family_scope():
    """Subsystem scope no longer separates tasks inside one project family.

    This inverts ``test_same_parent_never_erases_explicit_child_scope``: the
    数据中心 / 集控中心 distinction used to make two tasks incompatible so that a
    parent could not absorb a child's scope. Since 2026-09-17 they are one
    project, and separation has to come from a genuinely different family.
    """
    raw=native_item('新河二矿项目-数据中心')
    context=SourceContext('2026-04-07','S','generic',0,{**raw,'project':'新河二矿项目'}, {},None,[],{'status':'PASS','issues':[]},admission={'original_item':raw},input_stage='M1')
    sibling={'project':'新河二矿项目','project_entity_id':None,'source_project':'新河二矿项目-集控中心'}
    same={'project':'新河二矿项目','project_entity_id':None,'source_project':'新河二矿项目-数据中心'}
    configure_semantics(HashEmbedder(),family_lookup=family_lookup({
        '新河二矿项目':'FAM-XINHE','蓝川煤矿项目':'FAM-LANCHUAN'}))
    assert project_compatible(context,sibling)
    assert project_compatible(context,same)
    assert not project_compatible(context,{'project':'蓝川煤矿项目','project_entity_id':None})
    # A resolved family identity outranks name comparison entirely.
    resolved=replace(context,project_entity_id='FAMILY-FAM-XINHE')
    assert project_compatible(resolved,{'project':None,'project_entity_id':'FAMILY-FAM-XINHE'})
    assert not project_compatible(resolved,{'project':'新河二矿项目','project_entity_id':'FAMILY-FAM-LANCHUAN'})
    assert m6_project_compatible is project_compatible


def test_no_specific_project_is_not_an_uncertain_named_project():
    raw={**native_item(None),'item_type':'NON_PROJECT_WORK','title':'年度报告编制'}
    context=SourceContext('2026-04-07','S','generic',0,raw,{},None,[],{'status':'PASS','issues':[]},
        admission={'original_item':raw,'scope_validated':False},input_stage='M1')
    assert project_compatible(context,{'project':None,'project_entity_id':None})
    assert not project_compatible(context,{'project':'某具体项目','project_entity_id':'PARENT-X'})
