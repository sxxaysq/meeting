"""Platform-triggered synthetic integration test, using existing HTTP services."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, HTTPException, Body
from full_dataset import DATA as FULL_DATA, start as start_full, summary as full_summary

from service import ROOT, create_app
from M6_TaskManager.src.service import atomic_write_json

HERE = Path(__file__).resolve().parent
RUN_ID = 'E2E-M1-M2-M6-20260914'
TEXT = '''M1-M2-M6闭环测试会议纪要（合成测试数据）
会议日期：2026年9月14日
部门：闭环测试组
一、下周工作安排
1. 由测试员甲负责，在2026年9月15日前完成闭环测试日志检查，并提交检查记录。
'''


def build_app():
    app = create_app()
    test_dir = HERE / 'data/e2e'
    test_dir.mkdir(parents=True, exist_ok=True)
    test_app = create_app(test_dir / 'm6', allow_test_documents=True)
    test_repo = test_app.state.repository
    # Synthetic department is confined to the test database; no dispatch consumer exists.
    test_repo.upsert_department('E2E-ONLY', '闭环测试组', 'test://never-send')
    app.mount('/test', test_app)
    app.mount('/full', create_app(FULL_DATA / 'm6', FULL_DATA / 'm2_service/m2', allow_test_documents=True))
    scope_file = HERE / 'data/active_test_scope.json'
    report_path = test_dir / 'status.json'
    # ponytail: one worker and one fixed synthetic scenario; use a job queue for parallel suites.
    lock = threading.Lock()

    def initial():
        return {'run_id': RUN_ID, 'status': 'NOT_STARTED', 'stage': '', 'error': '',
                'm1_item_count': 0, 'm2_item_count': 0, 'm2_validation': '',
                'task_count': 0, 'event_count': 0, 'review_count': 0,
                'replay_verified': False, 'lineage_verified': False,
                'm6_database_isolated': True, 'm1_m2_source_scope': 'synthetic_document', 'updated_at': ''}

    def save(report, stage, status='RUNNING'):
        report.update(stage=stage, status=status, updated_at=datetime.now(timezone.utc).isoformat())
        atomic_write_json(report_path, report)

    def run():
        report = initial()
        try:
            with httpx.Client(timeout=600, trust_env=False) as http:
                def request(method, url, **kwargs):
                    response = http.request(method, url, **kwargs)
                    response.raise_for_status()
                    return response.json()

                save(report, 'M1_EXTRACT')
                m1_path = test_dir / 'm1.json'
                (test_dir / 'input.txt').write_text(TEXT, encoding='utf-8')
                if m1_path.exists():
                    m1 = json.loads(m1_path.read_text())
                else:
                    m1 = request('POST', 'http://127.0.0.1:18091/m1/extract',
                                 files={'file': (RUN_ID + '.txt', TEXT.encode(), 'text/plain')},
                                 data={'mode': 'generic'})
                    atomic_write_json(m1_path, m1)
                assert m1['items'], 'M1 returned zero items'
                report['m1_item_count'] = len(m1['items'])
                envelope = {'source_document_id': RUN_ID, 'file_name': RUN_ID + '.txt',
                            'meeting_date': '2026-09-14', 'mode': 'generic', 'items': m1['items']}
                save(report, 'M1_INGEST')
                ingested = request('POST', 'http://127.0.0.1:18090/m1/ingest', json=envelope)
                assert ingested['item_count'] == len(m1['items'])
                save(report, 'M2_CONSOLIDATE')
                m2_run = request('POST', 'http://127.0.0.1:18093/m2/consolidate',
                                 json={'source_document_id': RUN_ID, 'workers': 1})
                atomic_write_json(test_dir / 'm2-run.json', m2_run)
                m2 = json.loads((ROOT / 'integration/m2_service/data/m2' / (RUN_ID + '.m2.json')).read_text())
                atomic_write_json(test_dir / 'm2.json', m2)
                report.update(m2_item_count=len(m2['items']), m2_validation=m2['validation']['status'])
                source_indexes = [index for row in m2['merge_trace'] for index in row['source_indexes']]
                assert sorted(source_indexes) == list(range(len(m1['items']))), 'M2 lineage coverage mismatch'
                report['lineage_verified'] = True
                if m2['validation']['status'] != 'PASS':
                    report['error'] = 'M2 quality gate blocked this synthetic input; no override applied'
                    save(report, 'M2_GATE', 'BLOCKED')
                    return
                save(report, 'M6_PROCESS')
                body = {'source_document_id': RUN_ID, 'm2_payload': m2}
                result = request('POST', 'http://127.0.0.1:18096/test/m6/process', json=body)
                atomic_write_json(test_dir / 'm6-run.json', result)
                before = (len(test_repo.list_tasks()), len(test_repo.list_events()), len(test_repo.list_reviews()))
                report.update(task_count=before[0], event_count=before[1], review_count=before[2])
                assert before[0] >= 1 and before[1] >= 1 and before[2] == 0, 'M6 did not apply a task without review'
                save(report, 'IDEMPOTENCY_REPLAY')
                ingest_again = request('POST', 'http://127.0.0.1:18090/m1/ingest', json=envelope)
                m2_again = request('POST', 'http://127.0.0.1:18093/m2/consolidate', json={'source_document_id': RUN_ID})
                replay = request('POST', 'http://127.0.0.1:18096/test/m6/process', json=body)
                after = (len(test_repo.list_tasks()), len(test_repo.list_events()), len(test_repo.list_reviews()))
                assert ingest_again['item_ids'] == ingested['item_ids'] and ingest_again['item_count'] == ingested['item_count']
                assert m2_again['skipped'] and replay['idempotent_replay'] and before == after
                report['replay_verified'] = True
                save(report, 'COMPLETE', 'PASS')
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'[:1500]
            save(report, report['stage'], 'FAILED')
        finally:
            lock.release()

    @app.post('/m6/e2e-start')
    def start(background_tasks: BackgroundTasks, payload: dict = Body(default={})):
        if set(payload) - {'dataset'} or payload.get('dataset', 'synthetic') not in ('synthetic', 'full'):
            raise HTTPException(422, 'dataset must be synthetic or full')
        if payload.get('dataset') == 'full':
            atomic_write_json(scope_file, 'full')
            return start_full()
        atomic_write_json(scope_file, 'synthetic')
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'Synthetic E2E test is already running')
        report = initial()
        save(report, 'QUEUED')
        background_tasks.add_task(run)
        return report

    @app.get('/m6/e2e-status')
    def status():
        if scope_file.exists() and json.loads(scope_file.read_text()) == 'full':
            # Existing platform summary schema stays small; detailed evidence is retained on disk.
            return {key: value for key, value in full_summary().items() if key != 'documents'}
        return json.loads(report_path.read_text()) if report_path.exists() else initial()

    return app
