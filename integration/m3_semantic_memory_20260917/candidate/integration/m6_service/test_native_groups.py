import hashlib
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from native_full_dataset import independent_groups,build_service
from M6_TaskManager.src.repository import TaskRepository
from M6_TaskManager.src.source_admission import SourceAdmission
from M6_TaskManager.src.models import SourceContext


def test_parallel_only_independent_projects_matches_serial(tmp_path):
    def context(index,project,title,content,department='研发部'):
        raw={'department':department,'work_section':None,'delivery_group':None,'project':project,'item_type':'PROJECT_TASK',
            'assignee':[],'title':title,'content':content,'evidence':{'text':content,'page_start':1,'page_end':1,'start_char':index*100,'end_char':index*100+len(content),'exact_match':True}}
        return SourceContext('2026-04-07','S'+str(index),'generic',index,raw,
            {'source_indexes':[index],'source_evidence':[raw['evidence']],'merged':False},'RAW-'+project,[],{'status':'PASS','issues':[]},input_stage='M1')
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
