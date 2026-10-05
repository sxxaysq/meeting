"""Native M1 HTTP contract, finite recovery and idempotency; no M2 stage."""
import copy
import fcntl
import json
from fastapi.testclient import TestClient
from service import ROOT,create_app


class CountingClient:
    def __init__(self):self.calls=0
    def decide(self,*_args,**_kwargs):
        self.calls+=1
        return {'decision':'CREATE','target_index':None,'reason':'New task','evidence':None,'fields':[],'scope':'same_task'},{}


def test_http_contract_and_recovery(tmp_path):
    source=tmp_path/'m1';source.mkdir()
    legacy=json.loads((ROOT/'M6_TaskManager/examples/m2_pass.sample.json').read_text())
    payload={'items':legacy['items']}
    model=CountingClient();app=create_app(tmp_path/'state',source,model)
    repo=app.state.repository
    repo.upsert_department('D-TEST','智能矿山事业部','test://never-send')
    with TestClient(app) as http:
        assert http.get('/healthz').json()['pipeline']=='M1→M3→M6'
        assert http.get('/healthz').json()['m2_enabled'] is False
        assert http.post('/m6/process',json={'source_document_id':'legacy','m2_payload':legacy}).status_code==422
        assert http.post('/m6/process',json={'source_document_id':'legacy','m1_payload':legacy}).status_code==422
        invalid=copy.deepcopy(payload);invalid['items'][0]['unknown']='forbidden'
        assert http.post('/m6/process',json={'source_document_id':'bad','m1_payload':invalid}).status_code==422
        assert http.post('/m6/process',json={'source_document_id':'../escape'}).status_code==422
        assert http.post('/m6/process',json={'source_document_id':'missing'}).status_code==404
        assert http.post('/m6/process',json={'source_document_id':'E2E-production','m1_payload':payload}).status_code==422
        assert not repo.list_tasks() and model.calls==0
        body={'source_document_id':'2026-04-07-test','m1_payload':payload}
        first=http.post('/m6/process',json=body)
        assert first.status_code==200,first.text
        assert first.json()['pipeline']=='M1→M3→M6' and first.json()['action_counts']=={'CREATE':1}
        assert model.calls==1 and len(repo.list_tasks())==1
        assert http.post('/m6/process',json=body).json()['idempotent_replay']
        next((tmp_path/'state/results').glob('*.json')).unlink()
        assert http.post('/m6/process',json=body).json()['execution_status_counts']=={'DUPLICATE':1}
        changed=copy.deepcopy(body);changed['m1_payload']['items'][0]['title']+=' changed'
        assert http.post('/m6/process',json=changed).status_code==409
        with (tmp_path/'state/process.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            assert http.post('/m6/process',json=body).status_code==409
        assert http.get('/m6/export/tasks?limit=1').json()['total']==1
        assert http.get('/m6/export/sqlite_master').status_code==422
        (source/'2026-04-07-test.m1.json').write_text(json.dumps(payload))
        (source/'broken.m1.json').write_text(json.dumps(invalid))
        state=http.get('/m6/pending').json()
        assert state['pending_count']==0 and state['blocked_count']==1
        skipped=copy.deepcopy(payload);skipped['items'][0]['item_type']='NON_TASK_ITEM'
        (source/'2026-04-08-skip.m1.json').write_text(json.dumps(skipped))
        result=http.post('/m6/run-pending?limit=1')
        assert result.status_code==200 and result.json()['results'][0]['action_counts']=={'SKIP':1}
        assert model.calls==1 and len(repo.list_events())==1
    with TestClient(create_app(tmp_path/'state',source,model)) as restarted:
        assert restarted.post('/m6/process',json=body).json()['idempotent_replay']
        assert model.calls==1
