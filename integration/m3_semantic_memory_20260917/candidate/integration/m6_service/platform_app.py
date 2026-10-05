"""Platform trigger for the native M1→M3 retrieval→M6 decision/execution chain."""
import json
import threading
from datetime import datetime,timezone
from pathlib import Path
import httpx
from fastapi import BackgroundTasks,Body,HTTPException
from service import create_app
from native_full_dataset import start as start_full,summary as full_summary
from M6_TaskManager.src.service import atomic_write_json

HERE=Path(__file__).resolve().parent
RUN_ID='E2E-M1-M3-M6-2026-09-15'
TEXT='''M1-M3-M6闭环测试会议纪要（合成测试）
会议日期：2026年9月15日
部门：闭环测试组
下周工作安排：由测试员甲负责，在2026年9月16日前完成闭环测试日志检查，并提交检查记录。
'''


def build_app():
    app=create_app()
    directory=HERE/'data/native_e2e'
    directory.mkdir(parents=True,exist_ok=True)
    test_app=create_app(directory/'m6',allow_test_documents=True)
    repo=test_app.state.repository
    repo.upsert_department('E2E-ONLY','闭环测试组','test://never-send')
    app.mount('/test',test_app)
    lock=threading.Lock()
    scope_file=HERE/'data/active_test_scope.json'
    status_file=directory/'status.json'
    def initial():
        return {'run_id':RUN_ID,'pipeline':'M1→M3→M6','m2_enabled':False,'status':'NOT_STARTED',
            'stage':'','error':'','m1_item_count':0,'m3_item_count':0,'m2_item_count':0,'m2_validation':'DISABLED',
            'task_count':0,'event_count':0,'review_count':0,'replay_verified':False,'lineage_verified':False,
            'm6_database_isolated':True,'updated_at':''}
    def save(report,stage,status='RUNNING'):
        report.update(stage=stage,status=status,updated_at=datetime.now(timezone.utc).isoformat())
        atomic_write_json(status_file,report)
    def run():
        report=initial()
        try:
            with httpx.Client(timeout=600,trust_env=False) as http:
                save(report,'M1_EXTRACT')
                source=directory/'m1.json'
                if source.exists():m1=json.loads(source.read_text())
                else:
                    response=http.post('http://127.0.0.1:18091/m1/extract',
                        files={'file':(RUN_ID+'.txt',TEXT.encode(),'text/plain')},data={'mode':'generic'})
                    response.raise_for_status();m1={'items':response.json()['items'],'mode':'generic'}
                    atomic_write_json(source,m1)
                report['m1_item_count']=len(m1['items'])
                save(report,'M3_RETRIEVAL_M6_DECISION')
                body={'source_document_id':RUN_ID,'m1_payload':m1}
                response=http.post('http://127.0.0.1:18096/test/m6/process',json=body)
                response.raise_for_status();result=response.json()
                atomic_write_json(directory/'m6.json',result)
                assert result['pipeline']=='M1→M3→M6' and result['item_count']==len(m1['items'])
                report['m3_item_count']=result['item_count']
                before=(len(repo.list_tasks()),len(repo.list_events()),len(repo.list_reviews()))
                report.update(task_count=before[0],event_count=before[1],review_count=before[2])
                assert before[0]>=1 and before[1]>=1 and before[2]==0
                with repo.connect() as c:
                    proofs=[json.loads(r[0]) for r in c.execute('SELECT provenance_json FROM task_audit')]
                assert all(p['input_stage']=='M1' and not p['merge_trace']['merged'] for p in proofs)
                report['lineage_verified']=True
                save(report,'IDEMPOTENCY_REPLAY')
                response=http.post('http://127.0.0.1:18096/test/m6/process',json=body)
                response.raise_for_status()
                assert response.json()['idempotent_replay']
                assert before==(len(repo.list_tasks()),len(repo.list_events()),len(repo.list_reviews()))
                report['replay_verified']=True
                save(report,'COMPLETE','PASS')
        except Exception as error:
            report['error']=f'{type(error).__name__}: {error}'[:1500]
            save(report,report['stage'],'FAILED')
        finally:lock.release()
    @app.post('/m6/e2e-start')
    def start(background_tasks:BackgroundTasks,payload:dict=Body(default={})):
        if set(payload)-{'dataset'} or payload.get('dataset','synthetic') not in ('synthetic','full'):
            raise HTTPException(422,'dataset must be synthetic or full')
        if payload.get('dataset')=='full':
            atomic_write_json(scope_file,'native-full');return start_full()
        if not lock.acquire(blocking=False):raise HTTPException(409,'Native test already running')
        atomic_write_json(scope_file,'native-synthetic')
        report=initial();save(report,'QUEUED');background_tasks.add_task(run);return report
    @app.get('/m6/e2e-status')
    def status():
        if scope_file.exists() and json.loads(scope_file.read_text())=='native-full':
            return full_summary()
        return json.loads(status_file.read_text()) if status_file.exists() else initial()
    return app
