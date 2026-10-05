"""Run with the service's Python 3.11 environment; all writes are temporary."""
import copy
import fcntl
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from service import ROOT, create_app


class CountingClient:
    def __init__(self):
        self.calls = 0

    def decide(self, *args, **kwargs):
        self.calls += 1
        return {'decision': 'CREATE', 'target_index': None, 'reason': 'New task',
                'evidence': None, 'fields': [], 'scope':'same_task'}, {'model': 'test'}


def test_http_contract_and_recovery():
    with TemporaryDirectory() as temporary:
        base = Path(temporary)
        source = base / 'm2'
        source.mkdir()
        payload = json.loads((ROOT / 'M6_TaskManager/examples/m2_pass.sample.json').read_text())
        model = CountingClient()
        app = create_app(base / 'state', source, model)
        repo = app.state.repository
        repo.upsert_department('D-TEST', '智能矿山事业部', 'test://never-send')
        with TestClient(app) as http:
            assert http.get('/healthz').json()['status'] == 'ok'
            for status in ('REVIEW', 'ERROR'):
                invalid = copy.deepcopy(payload)
                invalid['validation']['status'] = status
                assert http.post('/m6/process', json={'source_document_id': status, 'm2_payload': invalid}).status_code == 422
            invalid = copy.deepcopy(payload)
            invalid['items'][0]['unknown'] = 'forbidden'
            assert http.post('/m6/process', json={'source_document_id': 'invalid', 'm2_payload': invalid}).status_code == 422
            assert http.post('/m6/process', json={'source_document_id': '../escape'}).status_code == 422
            assert http.post('/m6/process', json={'source_document_id': 'missing'}).status_code == 404
            assert http.post('/m6/process', json={'source_document_id': 'E2E-not-production', 'm2_payload': payload}).status_code == 422
            assert not repo.list_tasks() and model.calls == 0
            body = {'source_document_id': 'test-1', 'm2_payload': payload}
            first = http.post('/m6/process', json=body)
            assert first.status_code == 200, first.text
            assert first.json()['action_counts'] == {'CREATE': 1}, first.text
            assert model.calls == 1 and len(repo.list_tasks()) == 1
            assert http.post('/m6/process', json=body).json()['idempotent_replay'] is True
            assert model.calls == 1 and len(repo.list_events()) == 1
            # Simulate interruption after core commit but before result file persistence.
            next((base / 'state/results').glob('*.json')).unlink()
            resumed = http.post('/m6/process', json=body)
            assert resumed.json()['execution_status_counts'] == {'DUPLICATE': 1}
            assert model.calls == 1 and len(repo.list_events()) == 1
            changed = copy.deepcopy(body)
            changed['m2_payload']['items'][0]['title'] += ' changed'
            assert http.post('/m6/process', json=changed).status_code == 409
            with (base / 'state/process.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                assert http.post('/m6/process', json=body).status_code == 409
            exported = http.get('/m6/export/tasks?limit=1').json()
            assert exported['total'] == 1 and len(exported['rows']) == 1
            assert http.get('/m6/export/sqlite_master').status_code == 422
            assert http.get('/m6/result', params={'source_document_id': 'test-1'}).status_code == 200
            # Pending scans preserve REVIEW and skip completed inputs without model calls.
            (source / 'test-1.m2.json').write_text(json.dumps(payload))
            review = copy.deepcopy(payload)
            review['validation']['status'] = 'REVIEW'
            (source / 'review.m2.json').write_text(json.dumps(review))
            rejected_count = len(list((base / 'state/rejected').glob('*.json')))
            state = http.get('/m6/pending').json()
            assert state['pending_count'] == 0 and state['blocked_count'] == 1
            assert len(list((base / 'state/rejected').glob('*.json'))) == rejected_count
            assert http.post('/m6/run-pending?limit=1').json()['processed_count'] == 0
            assert model.calls == 1
            skipped = copy.deepcopy(payload)
            skipped['items'][0]['item_type'] = 'NON_TASK_ITEM'
            (source / 'test-2.m2.json').write_text(json.dumps(skipped))
            batch = http.post('/m6/run-pending?limit=1')
            assert batch.status_code == 200, batch.text
            assert batch.json()['results'][0]['action_counts'] == {'SKIP': 1}
            assert model.calls == 1
            # A process restart still replays the stored result without an LLM request.
            with TestClient(create_app(base / 'state', source, model)) as restarted:
                assert restarted.post('/m6/process', json=body).json()['idempotent_replay'] is True
                assert model.calls == 1
            mixed = copy.deepcopy(payload)
            independent = copy.deepcopy(payload['items'][0])
            independent.update(project=None, item_type='NON_PROJECT_WORK', title='独立例会', content='召开内部例会。')
            independent['evidence'].update(text='召开内部例会。', start_char=40, end_char=48)
            mixed['items'].append(independent)
            trace=copy.deepcopy(mixed['merge_trace'][0])
            trace.update(item_index=1,source_indexes=[1],source_evidence=[independent['evidence']],project_entity_id=None)
            mixed['merge_trace'].append(trace)
            mixed['validation']={'status':'REVIEW','issues':[{'code':'PROJECT_ALIAS_CONFLICT',
                'level':'review','message':'待复核','detail':{'item_indexes':[0]}}]}
            response=http.post('/m6/process',json={'source_document_id':'mixed-policy','m2_payload':mixed})
            assert response.status_code==200, response.text
            assert response.json()['action_counts']=={'REVIEW':1,'CREATE':1}, response.text


if __name__ == '__main__':
    test_http_contract_and_recovery()
    print('M6 HTTP contract, gating, replay, recovery and concurrency: PASS')
