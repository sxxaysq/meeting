from concurrent.futures import ThreadPoolExecutor
import json
import importlib.util
import sqlite3
from pathlib import Path
import native_full_dataset as native
from native_full_dataset import independent_groups,build_service
from M6_TaskManager.src.repository import TaskRepository
from M6_TaskManager.src.source_admission import SourceAdmission
from M6_TaskManager.src.models import SourceContext


def test_parallel_only_independent_projects_matches_serial(tmp_path):
    def context(index,project,title,content,department='研发部'):
        raw={'department':department,'work_section':None,'delivery_group':None,'project':project,'item_type':'PROJECT_TASK',
            'assignee':[],'title':title,'content':content,'evidence':{'text':content,'page_start':1,'page_end':1,'start_char':index*100,'end_char':index*100+len(content),'exact_match':True}}
        return SourceContext(source_document_id='2026-04-07',source_item_id='S'+str(index),
            source_mode='generic',item_index=index,item=raw,
            source_trace={'source_indexes':[index],'source_evidence':[raw['evidence']]},
            project_entity_id='RAW-'+project,input_validation={'status':'PASS','issues':[]},input_stage='M1')
    contexts=[context(0,'新河项目','机房施工','开始机房施工。'),context(1,'蓝川项目','平台测试','开始平台测试。'),
              context(2,'新河项目','机房施工','机房施工推进至第二阶段。','协作部')]
    class Model:
        def decide(self,_system,payload,**_kwargs):
            matching=[c for c in payload['historical_candidates'] if c['title']==payload['current_item']['title']]
            return {'decision':'PROGRESS_UPDATE' if matching else 'CREATE','target_index':matching[0]['index'] if matching else None,
                'fields':[],'scope':'same_task','reason':'fixture exact goal','evidence':None},{}
    results=[]
    for parallel in (False,True):
        repo=TaskRepository(tmp_path/(str(parallel)+'.db'));repo.initialize()
        for i,name in enumerate(('研发部','协作部')):repo.upsert_department(str(i),name,'test://never-send')
        admission=SourceAdmission(repo,None)
        for c in contexts:
            admission.rows[(c.source_document_id,c.source_item_id)]={'original_item':c.item,'parent_project':c.item['project'],
                'route':'M6','scope_validated':True,'parent_reason':'fixture M6 validated'}
        groups=independent_groups(contexts,admission,repo.list_tasks())
        assert sorted(len(g) for g in groups)==[1,2]
        def work(group):
            service=build_service(repo,admission,Model())
            return [service.process_context(c)['execution']['action'] for c in group]
        if parallel:
            with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(work,groups))
        else:work(contexts)
        results.append(sorted((t['project'],t['title'],t['description'],t['department'],t['status'],t['version']) for t in repo.list_tasks()))
        assert sum(t['version'] for t in repo.list_tasks())==3
    assert results[0]==results[1]


def test_native_batch_uses_current_m1_contract_and_idempotent_replay(tmp_path,monkeypatch):
    """Exercise the real batch/service/repository path with external services stubbed."""
    class Client:
        def __init__(self,**_kwargs):
            self.client=self
        def decide(self,_system,_payload,**_kwargs):
            return {'decision':'CREATE','target_index':None,'fields':[],
                    'scope':'same_task','reason':'fixture source requirement','evidence':None},{}
        def close(self):
            pass
    class Component:
        embedding_url='fixture://embedding'
        def __call__(self,*_args):
            return []
        def stats(self):
            return {'llm_calls':0}
        def export_snapshot(self):
            return {'fixture':True}
        def close(self):
            pass
    class Admission(SourceAdmission):
        def prepare(self,contexts):
            for context in contexts:
                self.rows[(context.source_document_id,context.source_item_id)]={
                    'original_item':context.item,'parent_project':context.item['project'],
                    'route':'M6','scope_validated':True,'parent_reason':'fixture identity'}
    component=Component()
    def layer(repository,client,_out,*,reset_memory):
        assert reset_memory is True
        return {'embedder':component,'memory':component,'resolver':component,
                'admission':Admission(repository,client),'reader':component,
                'reader_client':Client(),'index':component,'memory_before':{'fixture':True}}
    monkeypatch.setattr(native,'OpenAICompatibleLifecycleClient',Client)
    monkeypatch.setattr(native,'build_semantic_layer',layer)
    inputs=tmp_path/'inputs'
    document=inputs/'E2E-FULL-2026-04-08'
    document.mkdir(parents=True)
    content='本周开始机房施工。'
    item={'department':'研发部','work_section':None,'delivery_group':None,'project':'新河项目',
          'item_type':'PROJECT_TASK','assignee':[],'title':'机房施工','content':content,
          'evidence':{'text':content,'page_start':1,'page_end':1,'start_char':0,
                      'end_char':len(content),'exact_match':True}}
    (document/'m1.json').write_text(json.dumps({'items':[item]},ensure_ascii=False),encoding='utf-8')
    departments=tmp_path/'departments.json'
    departments.write_text(json.dumps([{'department_id':'TEST','name':'研发部','aliases_json':'[]'}]),encoding='utf-8')
    output=tmp_path/'replay'
    native.run_batch(inputs,departments,output)
    summary=json.loads((output/'summary.json').read_text(encoding='utf-8'))
    assert summary['pipeline']=='M1→M3→M6'
    assert summary['input_stage']=='M1'
    assert summary['complete'] and summary['replay_verified']
    assert summary['counts']['tasks']==1
    assert summary['counts']['processing_records']==1
    record=json.loads((output/document.name/'m6.json').read_text(encoding='utf-8'))['records'][0]
    assert record['execution']['action']=='CREATE'
    # The imported service must come from candidate even after frozen modules are removed.
    module=__import__('M6_TaskManager.src.service',fromlist=['__file__'])
    assert Path(module.__file__).resolve().is_relative_to(native.ROOT.resolve())
    with native.acquire_run_lock(output/'run.lock'):
        pass  # completion released the replay lock
    spec=importlib.util.spec_from_file_location('native_demo_export',native.ROOT.parent/'export_semantic_demo.py')
    export=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(export)
    monkeypatch.setattr(export,'SOURCE',output)
    monkeypatch.setattr(export,'TARGET',tmp_path/'demo.sqlite')
    monkeypatch.setattr(export,'METADATA',None)
    export.main()
    with sqlite3.connect(export.TARGET) as connection:
        assert connection.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
        assert connection.execute('SELECT count(*) FROM task_events').fetchone()[0]==1
        assert connection.execute('SELECT evidence_text FROM tasks').fetchone()[0]==content
        assert connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
