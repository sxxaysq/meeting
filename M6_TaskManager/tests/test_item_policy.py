import copy
import json

import pytest

from src.m2_input import load_m2_payload
from src.models import InputContractError
from src.input_policy import restriction
from .helpers import repository, context, historical_task, candidate, validator
from .test_service import service
from .test_command_validator import decision
from .test_m2_input import payload


def mixed():
    data = payload()
    other = copy.deepcopy(data['items'][0])
    other.update(project=None, item_type='NON_PROJECT_WORK', title='独立例会', content='召开内部例会。')
    other['evidence'].update(text='召开内部例会。', start_char=40, end_char=48)
    data['items'].append(other)
    trace = copy.deepcopy(data['merge_trace'][0])
    trace.update(item_index=1, source_indexes=[1], source_evidence=[other['evidence']], project_entity_id=None)
    data['merge_trace'].append(trace)
    data['validation'] = {'status':'REVIEW','issues':[{'code':'PROJECT_ENTITY_UNCERTAIN','level':'review',
        'message':'项目待确认','detail':{'item_indexes':[0],'project_names':['红沙泉二矿项目','红沙泉']}}]}
    return data


def test_mixed_document_and_replay(tmp_path):
    data = mixed(); path = tmp_path/'input.json'; path.write_text(json.dumps(data))
    repo = repository(tmp_path/'db.sqlite')
    workflow = service(repo,[{'decision':'CREATE','target_index':None,'reason':'独立事项',
                              'evidence':None,'fields':[],'scope':'same_task'}])
    result = workflow.process_file(path,output_path=tmp_path/'out.json',document_id='mixed')
    assert result['action_counts']=={'REVIEW':1,'CREATE':1}
    assert not result['records'][0]['llm_called']
    assert len(repo.list_tasks())==len(repo.list_reviews())==1
    replay=workflow.process_file(path,output_path=tmp_path/'out2.json',document_id='mixed')
    assert replay['execution_status_counts']=={'DUPLICATE':2}
    assert len(repo.list_tasks())==len(repo.list_reviews())==len(repo.list_events())==1


def test_operation_specific_limits(tmp_path):
    repo=repository(tmp_path/'db.sqlite'); repo.add_historical_task(historical_task())
    source=context()
    source.m2_validation.update(status='REVIEW',issues=[{'code':'MERGE_UNCERTAIN','level':'review',
        'message':'已拆开','detail':{'item_indexes':[0]}}])
    assert validator(repo).build(source,decision('CREATE'),[candidate(repo)]).action.value=='REVIEW'
    assert validator(repo).build(source,decision('PROGRESS_UPDATE','TASK-001'),[candidate(repo)]).action.value=='PROGRESS_UPDATE'
    source.m2_validation['issues'][0]['code']='PROJECT_ALIAS_CONFLICT'
    for action in ('CREATE','PROGRESS_UPDATE','MODIFY','COMPLETE','CANCEL','REOPEN','TRANSFER'):
        assert restriction(source,action)
    source.m2_validation['issues'][0]['code']='STRUCTURE_CONFLICT_VETO'
    assert restriction(source,'CREATE') is None
    source.m2_validation['issues'][0]['code']='UNKNOWN_REVIEW'
    source.m2_validation['issues'][0]['detail']={}
    assert 'UNSCOPED' in restriction(source,'CREATE')


def test_hard_errors_reject_before_writes(tmp_path):
    cases=[]
    d=mixed(); d['items'][0]['illegal']=True; cases.append(d)
    d=mixed(); d['merge_trace'][0]['source_evidence']=[]; cases.append(d)
    d=mixed(); d['merge_trace'][1]['source_indexes']=[0]; cases.append(d)
    d=mixed(); d['items'][0]['evidence']['start_char']=9999; cases.append(d)
    d=mixed(); d['validation']['status']='ERROR'; cases.append(d)
    d=mixed(); d['validation']['status']='PASS'; d['validation']['issues'][0]['level']='error'; cases.append(d)
    d=mixed(); d['validation']['issues'][0]['detail']['item_indexes']=[99]; cases.append(d)
    repo=repository(tmp_path/'db.sqlite'); workflow=service(repo,[])
    for i,d in enumerate(cases):
        path=tmp_path/f'bad-{i}.json'; path.write_text(json.dumps(d))
        with pytest.raises(InputContractError):
            workflow.process_file(path,output_path=tmp_path/f'out-{i}.json')
    assert repo.list_tasks()==repo.list_reviews()==repo.list_events()==[]


def test_pending_review_blocks_duplicate_create(tmp_path):
    repo=repository(tmp_path/'db.sqlite'); source=context()
    repo.add_historical_task(historical_task())
    source.m2_validation.update(status='REVIEW',issues=[{'code':'PROJECT_ENTITY_UNCERTAIN','level':'review',
        'message':'待确认','detail':{'item_indexes':[0]}}])
    service(repo,[]).process_context(source)
    other=context(source_item_id='different-week')
    command=validator(repo).build(other,decision('CREATE'),[])
    assert command.action.value=='REVIEW' and 'PENDING_REVIEW_DUPLICATE' in command.reason
    update=validator(repo).build(other,decision('PROGRESS_UPDATE','TASK-001'),[candidate(repo)])
    assert update.action.value=='PROGRESS_UPDATE'
    other.project_entities.append({'entity_id':other.project_entity_id,'aliases':[other.item['project']]})
    other.item['project']='新的规范项目名称'
    update=validator(repo).build(other,decision('PROGRESS_UPDATE','TASK-001'),[candidate(repo)])
    assert update.action.value=='PROGRESS_UPDATE'
