import json
from fastapi.testclient import TestClient
from service import create_app,ROOT


def test_retryable_result_is_not_cached_as_completed(tmp_path):
    class Model:
        failing=True
        def decide(self,*args,**kwargs):
            if self.failing:raise OSError('model unavailable')
            return {'decision':'CREATE','target_index':None,'fields':[],'scope':'same_task','reason':'new','evidence':None},{}
    model=Model()
    source=tmp_path/'m2';source.mkdir()
    payload=json.loads((ROOT/'M6_TaskManager/examples/m2_pass.sample.json').read_text())
    (source/'retry.m2.json').write_text(json.dumps(payload))
    app=create_app(tmp_path/'state',source,model)
    app.state.repository.upsert_department('D','智能矿山事业部','test://never-send')
    with TestClient(app) as http:
        first=http.post('/m6/process',json={'source_document_id':'retry'})
        assert first.status_code==503 and first.json()['retryable']
        assert not app.state.repository.list_reviews() and not app.state.repository.list_tasks()
        assert http.get('/m6/pending').json()['pending_count']==1
        attempted=http.post('/m6/run-pending').json()
        assert attempted['technical_failure_count']==1
        assert http.get('/m6/pending').json()['pending_count']==1
        model.failing=False
        second=http.post('/m6/process',json={'source_document_id':'retry'})
        assert second.status_code==200 and second.json()['action_counts']=={'CREATE':1}
        assert len(app.state.repository.list_tasks())==1
        assert http.post('/m6/process',json={'source_document_id':'retry'}).json()['idempotent_replay']
        assert http.get('/m6/pending').json()['pending_count']==0
