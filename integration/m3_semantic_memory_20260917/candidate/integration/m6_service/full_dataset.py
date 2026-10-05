"""Full PDF regression, isolated staging/catalog/lifecycle stores, resumable artifacts."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from service import ROOT
from M6_TaskManager.src.repository import TaskRepository
from M6_TaskManager.src.service import atomic_write_json

DATA = Path(__file__).resolve().parent / 'data/full_dataset_item_policy_v2_20260914'
RUN_ID = 'E2E-FULL-ITEM-POLICY-V2-20260914'


def inventory():
    rows = []
    for path in (ROOT / '数据集').glob('*.pdf'):
        match = re.match(r'(\d{4})\.(\d{1,2})\.(\d{1,2})', path.name)
        if not match:
            raise ValueError('Cannot determine chronological order: ' + path.name)
        year, month, day = map(int, match.groups())
        date = f'{year:04}-{month:02}-{day:02}'
        rows.append({'file_name': path.name, 'meeting_date': date,
                     'source_document_id': 'E2E-FULL-' + date,
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    if not rows:
        raise ValueError('No PDF dataset found')
    return sorted(rows, key=lambda row: row['meeting_date'])


def summary():
    path = DATA / 'status.json'
    return json.loads(path.read_text()) if path.exists() else {
        'run_id': RUN_ID, 'status': 'NOT_STARTED', 'stage': 'FULL_DATASET',
        'error': '', 'm1_item_count': 0, 'm2_item_count': 0, 'm2_validation': '',
        'task_count': 0, 'event_count': 0, 'review_count': 0,
        'replay_verified': False, 'lineage_verified': False,
        'm6_database_isolated': True, 'm1_m2_source_scope': 'isolated_full_dataset',
        'updated_at': '', 'document_count': len(inventory()), 'completed_count': 0}


def start():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / 'worker.log').open('ab') as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve())],
                                 cwd=Path(__file__).parent, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return {**summary(), 'status': 'RUNNING', 'stage': 'FULL_DATASET_DISPATCHED', 'worker_pid': child.pid}


def ensure_services():
    env = {**os.environ, 'LLM_BASE_URL': 'http://192.168.30.215:8000/v1',
           'LLM_MODEL': 'qwen3.8-27b', 'LLM_API_KEY': os.getenv('LLM_API_KEY', 'EMPTY'),
           'LLM_ENABLE_THINKING': 'false', 'LLM_TEMPERATURE': '0',
           'LLM_MAX_TOKENS': '8192', 'LLM_TRANSPORT': 'openai', 'LLM_TIMEOUT': '900',
           'M1_STAGING_DSN': 'sqlite:///' + str(DATA / 'staging.sqlite'),
           'M2_DSN': 'sqlite:///' + str(DATA / 'staging.sqlite'),
           'M2_DATA_DIR': str(DATA / 'm2_service'), 'M2_CATALOG_PATH': str(DATA / 'project_catalog.sqlite')}
    for name, port in [('m1_staging', 18190), ('m2_service', 18193)]:
        with httpx.Client(timeout=2, trust_env=False) as http:
            try:
                health = http.get(f'http://127.0.0.1:{port}/healthz').json()
                assert health['dialect'] == 'sqlite'
                continue
            except httpx.ConnectError:
                pass
        with (DATA / (name + '.log')).open('ab') as log:
            child = subprocess.Popen(['/home/yty-s/venv/bin/python', '-m', 'uvicorn', 'service:app',
                                      '--host', '127.0.0.1', '--port', str(port)],
                                     cwd=ROOT / 'integration' / name, env=env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        (DATA / (name + '.pid')).write_text(str(child.pid))
        for _ in range(40):
            try:
                response = httpx.get(f'http://127.0.0.1:{port}/healthz', timeout=2, trust_env=False)
                response.raise_for_status()
                assert response.json()['dialect'] == 'sqlite'
                break
            except httpx.ConnectError:
                if child.poll() is not None:
                    raise RuntimeError(name + ' startup failed; inspect isolated log')
                time.sleep(0.25)
        else:
            raise RuntimeError(name + ' startup timeout')


def run():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        files = inventory()
        manifest = DATA / 'manifest.json'
        if manifest.exists() and json.loads(manifest.read_text()) != files:
            raise RuntimeError('Dataset changed since this run was started')
        atomic_write_json(manifest, files)
        (DATA / 'documents').mkdir(exist_ok=True)
        report = summary()
        rows = []
        repo = TaskRepository(DATA / 'm6/lifecycle.sqlite')
        repo.initialize()

        def save(stage, status='RUNNING'):
            report.update(stage=stage, status=status, document_count=len(files), completed_count=len(rows),
                          m1_item_count=sum(r.get('m1_item_count', 0) for r in rows),
                          m2_item_count=sum(r.get('m2_item_count', 0) for r in rows),
                          task_count=len(repo.list_tasks()), event_count=len(repo.list_events()),
                          review_count=len(repo.list_reviews()),
                          updated_at=datetime.now(timezone.utc).isoformat(), documents=rows)
            atomic_write_json(DATA / 'status.json', report)

        save('STARTING_ISOLATED_SERVICES')
        try:
            ensure_services()
            def extract(info):
                folder = DATA / 'documents' / info['source_document_id']
                folder.mkdir(exist_ok=True)
                cached = folder / 'm1.json'
                if cached.exists():
                    return json.loads(cached.read_text())
                started = time.monotonic()
                with httpx.Client(timeout=1800, trust_env=False) as extraction_http:
                    with (ROOT / '数据集' / info['file_name']).open('rb') as stream:
                        response = extraction_http.post('http://127.0.0.1:18091/m1/extract',
                            files={'file': (info['file_name'], stream, 'application/pdf')}, data={'mode': 'generic'})
                    response.raise_for_status()
                    value = response.json()
                assert value['items'], 'M1 returned zero items'
                atomic_write_json(cached, value)
                atomic_write_json(folder / 'm1-timing.json', {'seconds': round(time.monotonic()-started, 2)})
                return value

            # Only stateless extraction is prefetched; catalogs and lifecycle history remain ordered.
            with ThreadPoolExecutor(max_workers=2) as pool, httpx.Client(timeout=1800, trust_env=False) as http:
                extractions = {info['source_document_id']: pool.submit(extract, info) for info in files}
                def call(method, url, **kwargs):
                    response = http.request(method, url, **kwargs)
                    response.raise_for_status()
                    return response.json()

                for index, info in enumerate(files):
                    doc = info['source_document_id']
                    folder = DATA / 'documents' / doc
                    folder.mkdir(exist_ok=True)
                    finished = folder / 'result.json'
                    if finished.exists() and json.loads(finished.read_text())['status'] != 'FAILED':
                        rows.append(json.loads(finished.read_text()))
                        save(f'{index+1}/{len(files)} RESUMED')
                        continue
                    row = {**info, 'status': 'RUNNING', 'error': ''}
                    started = time.monotonic()
                    try:
                        save(f'{index+1}/{len(files)} M1 {info["meeting_date"]}')
                        m1 = extractions[doc].result()
                        assert m1['items'], 'M1 returned zero items'
                        row['m1_item_count'] = len(m1['items'])
                        envelope = {'source_document_id': doc, 'file_name': info['file_name'],
                                    'meeting_date': info['meeting_date'], 'mode': 'generic', 'items': m1['items']}
                        first = call('POST', 'http://127.0.0.1:18190/m1/ingest', json=envelope)
                        second = call('POST', 'http://127.0.0.1:18190/m1/ingest', json=envelope)
                        assert first['item_ids'] == second['item_ids'] and first['item_count'] == second['item_count']
                        row['m1_replay_verified'] = True
                        save(f'{index+1}/{len(files)} M2 {info["meeting_date"]}')
                        m2_run = call('POST', 'http://127.0.0.1:18193/m2/consolidate', json={'source_document_id': doc})
                        atomic_write_json(folder / 'm2-run.json', m2_run)
                        m2 = json.loads((DATA / 'm2_service/m2' / (doc + '.m2.json')).read_text())
                        atomic_write_json(folder / 'm2.json', m2)
                        row.update(m2_item_count=len(m2['items']), m2_validation=m2['validation']['status'],
                                   m2_issue_counts=m2['validation'].get('issue_counts', {}))
                        indexes = [i for trace in m2['merge_trace'] for i in trace['source_indexes']]
                        assert sorted(indexes) == list(range(len(m1['items']))), 'M2 lineage coverage mismatch'
                        row['lineage_verified'] = True
                        again = call('POST', 'http://127.0.0.1:18193/m2/consolidate', json={'source_document_id': doc})
                        assert again['skipped']
                        row['m2_replay_verified'] = True
                        save(f'{index+1}/{len(files)} M6 {info["meeting_date"]}')
                        body = {'source_document_id': doc, 'm2_payload': m2}
                        before = (len(repo.list_tasks()), len(repo.list_events()), len(repo.list_reviews()))
                        if row['m2_validation'] == 'ERROR':
                            response = http.post('http://127.0.0.1:18096/full/m6/process', json=body)
                            assert response.status_code == 422, 'M6 did not reject non-PASS input'
                            assert before == (len(repo.list_tasks()), len(repo.list_events()), len(repo.list_reviews()))
                            atomic_write_json(folder / 'm6-gate.json', response.json())
                            row.update(status='BLOCKED_ERROR', m6_http_status=422, m6_gate_verified=True)
                        else:
                            # Test fixtures use exact department names; these are never production routes.
                            for item in m2['items']:
                                name = item.get('department')
                                if name:
                                    repo.upsert_department('TEST-'+hashlib.sha256(name.encode()).hexdigest()[:16],
                                                           name, 'test://never-send')
                            result = call('POST', 'http://127.0.0.1:18096/full/m6/process', json=body)
                            atomic_write_json(folder / 'm6.json', result)
                            counts = (len(repo.list_tasks()), len(repo.list_events()), len(repo.list_reviews()))
                            replay = call('POST', 'http://127.0.0.1:18096/full/m6/process', json=body)
                            assert replay['idempotent_replay'] and counts == (len(repo.list_tasks()), len(repo.list_events()), len(repo.list_reviews()))
                            row.update(status='PASS' if counts[2] == before[2] else 'M6_REVIEW',
                                       m6_action_counts=result['action_counts'], m6_replay_verified=True,
                                       new_tasks=counts[0]-before[0], new_events=counts[1]-before[1],
                                       new_reviews=counts[2]-before[2])
                    except Exception as error:
                        row.update(status='FAILED', error=f'{type(error).__name__}: {error}'[:1500])
                    row['elapsed_seconds'] = round(time.monotonic()-started, 2)
                    atomic_write_json(folder / 'result.json', row)
                    rows.append(row)
                    save(f'{index+1}/{len(files)} FINISHED')
            report['replay_verified'] = all(r.get('m1_replay_verified') and r.get('m2_replay_verified') and
                                            (r.get('m6_replay_verified') or r.get('m6_gate_verified')) for r in rows)
            report['lineage_verified'] = all(r.get('lineage_verified') for r in rows)
            report['m2_validation'] = ', '.join(f'{state}:{sum(r.get("m2_validation")==state for r in rows)}' for state in ('PASS','REVIEW','ERROR'))
            report['error'] = '; '.join(f'{state}:{sum(r["status"]==state for r in rows)}' for state in ('FAILED','BLOCKED_ERROR','M6_REVIEW'))
            report['test_execution'] = 'COMPLETED'
            save('FULL_DATASET_COMPLETE', 'PASS' if all(r['status']=='PASS' for r in rows) else 'COMPLETED_WITH_ISSUES')
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'[:1500]
            save(report['stage'], 'FAILED')


if __name__ == '__main__':
    run()
