import json
from dataclasses import replace
import pytest
from src.candidate_retriever import CandidateRetriever
from src.executor import TaskExecutor
from src.lifecycle_judge import LifecycleJudge
from src.models import ExecutionConflict,DecisionValidationError
from M3_KnowledgeGraph.src.task_retrieval import configure_semantics,reset_semantics
from .helpers import (repository,context,item,historical_task,candidate,validator,
    ScriptedReader,HashEmbedder,family_lookup,COMPLETION_UNPROVEN,STATUS_NEGATED)
from .test_command_validator import decision
from .test_service import service


@pytest.fixture(autouse=True)
def _isolate_semantics():
    """task_retrieval keeps a module-level embedder/family lookup; never leak it."""
    reset_semantics()
    yield
    reset_semantics()


def proposal(action='PROGRESS_UPDATE',target=0,**kwargs):
    return {'decision':action,'target_index':target,'fields':[],'scope':'same_task',
            'reason':'相同目标有直接原文依据','evidence':None,**kwargs}


def test_unrelated_pending_project_work_does_not_spread(tmp_path):
    repo=repository(tmp_path/'t.db')
    review=context(current_item=item(title='建设数据中心'))
    TaskExecutor(repo).execute(validator(repo).review_for_failure(review,[],'PROJECT_ALIAS_CONFLICT'))
    fresh=context(current_item=item(title='新建火灾监测系统',content='新建火灾监测系统。'),source_item_id='next')
    assert validator(repo).build(fresh,decision('CREATE'),[]).action.value=='CREATE'
    duplicate=replace(review,source_item_id='repeat')
    assert validator(repo).build(duplicate,decision('CREATE'),[]).action.value=='REVIEW'


def test_incompatible_projects_never_fall_back_to_global_tasks(tmp_path):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task())
    source=context(current_item=item(project='红沙泉一矿项目'),project_entity_id='P-OTHER')
    assert CandidateRetriever(repo).retrieve(source)==[]
    assert validator(repo).build(source,decision('CREATE'),[]).action.value=='CREATE'
    with pytest.raises(DecisionValidationError):
        validator(repo).build(source,decision('PROGRESS_UPDATE','TASK-001'),[candidate(repo)])


def test_stale_unavailable_and_foreign_vector_hits_fall_back(tmp_path):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task())
    expected=CandidateRetriever(repo).retrieve(context())
    for hits in [[{'task_id':'TASK-001','version':0,'last_event_id':None,'score':1}],
                 [{'task_id':'FUTURE-OR-PRODUCTION-TASK','version':1,'score':1}]]:
        retriever=CandidateRetriever(repo,semantic_search=lambda *_:hits)
        assert retriever.retrieve(context())==expected
        assert retriever.index_diagnostics
    def unavailable(*_):raise OSError('index unavailable')
    assert CandidateRetriever(repo,semantic_search=unavailable).retrieve(context())==expected


def test_collaborative_progress_changes_version_not_owner(tmp_path):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task())
    source=context(current_item=item(department='研发中心',content='配合完成接口联调。'))
    command=validator(repo).build(source,decision('PROGRESS_UPDATE','TASK-001'),[candidate(repo)])
    before=repo.get_task('TASK-001');TaskExecutor(repo).execute(command)
    after=repo.get_task('TASK-001')
    assert after['department_id']==before['department_id']=='D-IM'
    assert after['description']==before['description']+'\n'+source.item['content'] and after['version']==2
    assert TaskExecutor(repo).execute(command)['execution_status']=='DUPLICATE'
    assert len(repo.list_events())==1


def test_child_completion_does_not_close_parent(tmp_path):
    repo=repository(tmp_path/'t.db')
    repo.add_historical_task(historical_task(title='红沙泉二矿智能化建设',description='建设数据中心、集控中心和管理平台。'))
    source=context(current_item=item(title='数据中心建设',content='数据中心建设已全部完成。'))
    # The child is genuinely finished; what is not finished is the whole goal.
    reader=ScriptedReader(completion={'definite':True,'future_or_partial':False,
        'whole_goal':False,'phases_covered':False,'goal_in_evidence':True})
    command=validator(repo,reader).build(source,decision('COMPLETE','TASK-001',completion_evidence=source.item['content']),[candidate(repo)])
    assert command.action.value=='PROGRESS_UPDATE'
    TaskExecutor(repo).execute(command)
    assert repo.get_task('TASK-001')['status']!='COMPLETED'


def test_whole_task_completion_and_unsupported_evidence(tmp_path):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task(title='建设数据中心'))
    source=context(current_item=item(content='已完成数据中心建设。'))
    command=validator(repo).build(source,decision('COMPLETE','TASK-001',completion_evidence=source.item['content']),[candidate(repo)])
    assert command.action.value=='COMPLETE'
    with pytest.raises(DecisionValidationError):
        validator(repo).build(source,decision('COMPLETE','TASK-001',completion_evidence='虚构验收通过'),[candidate(repo)])


def test_modify_preserves_existing_requirements_and_no_change_becomes_progress(tmp_path):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task())
    source=context(current_item=item(content='新增双机容灾要求。'))
    before=repo.get_task('TASK-001')['description']
    command=validator(repo).build(source,decision('MODIFY','TASK-001',changes={'description':source.item['content']}),[candidate(repo)])
    TaskExecutor(repo).execute(command)
    assert repo.get_task('TASK-001')['description']==before+'\n'+source.item['content']
    source=replace(source,source_item_id='second',item=item(content='继续开展联调。'))
    assert validator(repo).build(source,decision('MODIFY','TASK-001',changes={}),[candidate(repo)]).action.value=='PROGRESS_UPDATE'


def test_version_change_reloads_then_rejudges(tmp_path,monkeypatch):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task())
    workflow=service(repo,[proposal(),proposal()])
    original=workflow.executor.execute; attempts=[]
    def racing(command):
        attempts.append(command.expected_version)
        if len(attempts)==1:
            with repo.connect() as c:
                c.execute("UPDATE tasks SET version=version+1 WHERE task_id='TASK-001'");c.commit()
        return original(command)
    monkeypatch.setattr(workflow.executor,'execute',racing)
    result=workflow.process_context(context())
    assert result['execution']['action']=='PROGRESS_UPDATE'
    assert attempts==[1,2] and repo.get_task('TASK-001')['version']==3
    assert 'version_refresh' in result['model_audit']['recovery']


def test_model_service_failure_is_retryable_not_review(tmp_path):
    repo=repository(tmp_path/'t.db')
    workflow=service(repo,[])
    result=workflow.process_context(context())
    assert result['failure_kind']=='TECHNICAL_FAILURE'
    assert not repo.list_reviews() and not repo.list_tasks()
    assert repo.get_processing_record(context().source_document_id,context().source_item_id) is None
    recovered=service(repo,[proposal('CREATE',None)]).process_context(context())
    assert recovered['execution']['action']=='CREATE'


def test_format_repair_does_not_become_business_review(tmp_path):
    repo=repository(tmp_path/'t.db')
    workflow=service(repo,[{'sql':'DROP TABLE tasks'},proposal('CREATE',None)])
    result=workflow.process_context(context())
    assert result['execution']['action']=='CREATE' and not repo.list_reviews()
    assert result['model_audit']['format_retries']==1 and result['model_audit']['calls']==2


def test_one_expansion_and_no_program_fields_in_model_payload(tmp_path):
    repo=repository(tmp_path/'t.db')
    for n in range(8):repo.add_historical_task(historical_task(task_id=f'TASK-{n}'))
    workflow=service(repo,[proposal('EXPAND',None),proposal()])
    result=workflow.process_context(context())
    assert result['execution']['action']=='PROGRESS_UPDATE'
    assert result['model_audit']['recovery']==['expanded_retrieval']
    payload=LifecycleJudge._payload(context(),[candidate(repo,'TASK-0')])
    assert not {'task_id','version','department_id','department_route'} & set(payload['historical_candidates'][0])


@pytest.mark.parametrize('action,text',[('CANCEL','不要取消数据中心建设任务。'),
    ('REOPEN','暂不重新启动数据中心建设任务。'),('TRANSFER','不要移交研发中心，保留当前责任部门。')])
def test_negated_state_change_never_executes(tmp_path,action,text):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task())
    source=context(current_item=item(content=text))
    kwargs={'completion_evidence':text}
    if action=='TRANSFER':kwargs['department_change']={'to_department':'研发中心','from_department':'智能矿山事业部','evidence':text}
    with pytest.raises(DecisionValidationError):
        validator(repo,ScriptedReader(status=STATUS_NEGATED)).build(source,decision(action,'TASK-001',**kwargs),[candidate(repo)])
    assert not repo.list_events()


def test_subtask_requirement_does_not_modify_parent_definition(tmp_path):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task())
    source=context(current_item=item(content='新增一个仪表盘指标。'))
    command=validator(repo).build(source,decision('MODIFY','TASK-001',scope='subtask',changes={'description':source.item['content']}),[candidate(repo)])
    assert command.action.value=='PROGRESS_UPDATE'


def test_negated_whole_completion_remains_progress(tmp_path):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task(title='建设数据中心'))
    source=context(current_item=item(content='数据中心建设未全部完成。'))
    reader=ScriptedReader(completion={'definite':False,'future_or_partial':True,
        'whole_goal':True,'phases_covered':True,'goal_in_evidence':True})
    command=validator(repo,reader).build(source,decision('COMPLETE','TASK-001',completion_evidence=source.item['content']),[candidate(repo)])
    assert command.action.value=='PROGRESS_UPDATE'


def test_year_conflict_is_a_memory_fact_not_a_regex(tmp_path):
    """Annual cycles stay distinct because the memory graph resolved two families.

    The previous implementation caught this with a ``20\\d{2}`` qualifier regex
    that overrode a shared entity id. The regex is gone; the distinction now has
    to come from resolution, and a bogus shared id no longer survives it.
    """
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task(project='2025 网络改造项目'))
    source=context(current_item=item(project='2026 网络改造项目'))
    configure_semantics(HashEmbedder(),family_lookup=family_lookup({
        '2025 网络改造项目':'FAM-NET-2025','2026 网络改造项目':'FAM-NET-2026'}))
    # Both sides carry the same bogus entity id; the resolved family disagrees.
    admitted=replace(source,project_entity_id=None)
    assert CandidateRetriever(repo).retrieve(admitted)==[]


def test_same_family_recalls_child_scope_task(tmp_path):
    """A subsystem task is recalled once the memory graph puts both in one family."""
    repo=repository(tmp_path/'t.db')
    repo.add_historical_task(historical_task(title='红沙泉二矿现场实施跟进',project_entity_id='FAMILY-HSQ'))
    repo.add_historical_task(historical_task(task_id='CHILD',project_entity_id='FAMILY-HSQ',
        project='红沙泉二矿项目-集控中心',title='红沙泉二矿集控中心装修及空调到货',description='集控中心空调设备到货。'))
    source=context(current_item=item(title='红沙泉二矿集控中心设备安装推进',content='推进集控中心空调及新风设备安装至80%。'),
        project_entity_id='FAMILY-HSQ')
    configure_semantics(HashEmbedder(),family_lookup=family_lookup({
        '红沙泉二矿项目':'FAM-HSQ','红沙泉二矿项目-集控中心':'FAM-HSQ'}))
    candidates=CandidateRetriever(repo).retrieve(source)
    assert candidates[0]['task_id']=='CHILD'
    assert validator(repo).build(source,decision('PROGRESS_UPDATE','CHILD'),candidates).action.value=='PROGRESS_UPDATE'
    # Without the resolved family the two ids disagree and the write is refused.
    stranger=replace(source,project_entity_id='FAMILY-OTHER')
    with pytest.raises(DecisionValidationError):
        validator(repo).build(stranger,decision('PROGRESS_UPDATE','CHILD'),candidates)


def test_sibling_subsystems_stay_incompatible(tmp_path):
    repo=repository(tmp_path/'t.db')
    repo.add_historical_task(historical_task(project='红沙泉二矿项目-数据中心'))
    source=context(current_item=item(project='红沙泉二矿项目-集控中心'),project_entity_id='OTHER')
    assert CandidateRetriever(repo).retrieve(source)==[]


def test_noop_with_new_source_fact_is_preserved_as_progress(tmp_path):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task())
    source=context(current_item=item(content='新增空调联调要求。'))
    command=validator(repo).build(source,decision('SKIP','TASK-001'),[candidate(repo)])
    assert command.action.value=='PROGRESS_UPDATE' and command.event_content==source.item['content']


def test_completion_of_one_phase_does_not_close_composite_goal(tmp_path):
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task(title='数据中心装修及安装'))
    source=context(current_item=item(title='数据中心装修',content='已完成数据中心装修。'))
    reader=ScriptedReader(completion={'definite':True,'future_or_partial':False,
        'whole_goal':True,'phases_covered':False,'goal_in_evidence':True})
    command=validator(repo,reader).build(source,decision('COMPLETE','TASK-001',completion_evidence=source.item['content']),[candidate(repo)])
    assert command.action.value=='PROGRESS_UPDATE'


@pytest.mark.parametrize('action,text,quote',[('COMPLETE','数据中心建设未全部完成。','全部完成'),
    ('CANCEL','不要取消数据中心建设任务。','取消数据中心建设任务'),
    ('TRANSFER','不要移交研发中心负责。','移交研发中心负责')])
def test_evidence_cannot_trim_away_negation(tmp_path,action,text,quote):
    """The reader is handed the surrounding text, so quoting a fragment cannot
    hide the 不/未 that precedes it. That guarantee is structural and survives the
    removal of the negation regexes; what the reader does with it is its own."""
    repo=repository(tmp_path/'t.db');repo.add_historical_task(historical_task(title='建设数据中心'))
    source=context(current_item=item(content=text))
    kwargs={'completion_evidence':quote}
    if action=='TRANSFER':kwargs['department_change']={'to_department':'研发中心','evidence':quote}
    reader=ScriptedReader(completion=COMPLETION_UNPROVEN,status=STATUS_NEGATED)
    if action=='COMPLETE':
        assert validator(repo,reader).build(source,decision(action,'TASK-001',**kwargs),[candidate(repo)]).action.value=='PROGRESS_UPDATE'
    else:
        with pytest.raises(DecisionValidationError):validator(repo,reader).build(source,decision(action,'TASK-001',**kwargs),[candidate(repo)])
    kind,payload=reader.calls[0]
    assert kind==('COMPLETION' if action=='COMPLETE' else action)
    quoted=payload.get('context') or ''
    position=text.find(quote)
    assert position>0 and text[position-1] in quoted, '引文前一个字必须进入判读上下文'
    assert len(quoted)>len(quote), '判读器不能只拿到引文本身'
