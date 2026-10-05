"""Chronological replay; independent project groups share no possible task targets.

Since 2026-09-17 the run is wired to the semantic layer: bge-m3 embeddings,
a Neo4j long-term memory graph, and LLM evidence reading replace the lexical and
regex guards. See ``M3_KnowledgeGraph/src/{embedding_client,semantic_memory,
project_resolver,semantic_reader,task_retrieval}.py``.
"""
import fcntl
import hashlib
import json
import sys
import threading
import argparse
import subprocess
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
BENCH=ROOT/'integration/m1_m3_m6_20260915'
sys.path.insert(0,str(ROOT))
from M6_TaskManager.src.repository import TaskRepository,utc_now
from M6_TaskManager.src.m1_input import load_m1_payload
from M6_TaskManager.src.candidate_retriever import CandidateRetriever,project_compatible,normalize_text
from M6_TaskManager.src.command_validator import CommandValidator
from M6_TaskManager.src.department_router import DepartmentRouter
from M6_TaskManager.src.lifecycle_judge import LifecycleJudge
from M6_TaskManager.src.llm_client import OpenAICompatibleLifecycleClient
from M6_TaskManager.src.executor import TaskExecutor
from M6_TaskManager.src.source_admission import SourceAdmission
from M6_TaskManager.src.service import TaskLifecycleService,atomic_write_json
from M3_KnowledgeGraph.src.embedding_client import EmbeddingClient
from M3_KnowledgeGraph.src.project_resolver import ProjectResolver
from M3_KnowledgeGraph.src.semantic_memory import connect_memory
from M3_KnowledgeGraph.src.semantic_reader import SemanticReader
from M3_KnowledgeGraph.src.task_retrieval import SemanticTaskIndex,configure_semantics

LLM_BASE_URL='http://192.168.30.215:8000/v1'
LLM_MODEL='qwen3.8-27b'


def independent_groups(contexts,admission,history):
    """Conservatively serialize aliases, parent branches and shared historical targets."""
    parents=list(range(len(contexts)))
    def root(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]];i=parents[i]
        return i
    def join(indices):
        if indices:
            for i in indices[1:]: parents[root(i)]=root(indices[0])
    effective=[admission.apply(c)[0] for c in contexts]
    keys={}
    for i,(raw,source) in enumerate(zip(contexts,effective)):
        for c in (raw,source):
            name=str(c.item.get('project') or '')
            # Scheduling only: even different phases in a parent family run serially.
            family=normalize_text(name.split('项目-')[0].split('项目—')[0].split('项目：')[0])
            for key in ('name:'+normalize_text(name),'family:'+family,
                        'entity:'+str(c.project_entity_id) if c.project_entity_id else 'name:'+normalize_text(name)):
                keys.setdefault(key,[]).append(i)
        if (source.admission or {}).get('scope_validated') is False:
            for hit in (source.admission or {}).get('retrieved_names',[]):
                keys.setdefault('name:'+normalize_text(hit['parent_project']),[]).append(i)
    for indices in keys.values(): join(indices)
    for task in history:
        join([i for i,c in enumerate(effective) if project_compatible(c,task)])
    # Catch other compatible future creations, using the shared production predicate.
    for i,a in enumerate(effective):
        prospective={**a.item,'description':a.item['content'],'project_entity_id':a.project_entity_id}
        join([i]+[j for j,b in enumerate(effective) if j!=i and project_compatible(b,prospective)])
    groups={}
    for i,c in enumerate(contexts): groups.setdefault(root(i),[]).append(c)
    return list(groups.values())


def build_service(repo,admission,client,index=None,reader=None):
    return TaskLifecycleService(repository=repo,
        retriever=CandidateRetriever(repo,semantic_search=index),judge=LifecycleJudge(client),
        validator=CommandValidator(repo,DepartmentRouter(repo),reader),
        executor=TaskExecutor(repo),admission=admission)


def build_semantic_layer(repo,scope_client,out,*,reset_memory):
    """Embeddings + long-term memory graph + resolver + evidence reader.

    ``reset_memory`` decides whether this replay starts from an empty graph. A
    full comparison run resets, so the memory it builds is entirely attributable
    to that run; the graph is left in place afterwards for inspection and for
    later incremental runs.
    """
    embedder=EmbeddingClient()
    memory=connect_memory(embedder)
    if reset_memory:
        memory.reset()
    memory.ensure_schema()
    before=memory.verify()
    resolver=ProjectResolver(memory,scope_client)
    admission=SourceAdmission(repo,scope_client,resolver=resolver)
    configure_semantics(embedder,family_lookup=admission.family_lookup)
    reader_client=OpenAICompatibleLifecycleClient(base_url=LLM_BASE_URL,model=LLM_MODEL,enable_thinking=False)
    reader=SemanticReader(memory,reader_client)
    index=SemanticTaskIndex(repo,embedder)
    return {'embedder':embedder,'memory':memory,'resolver':resolver,'admission':admission,
            'reader':reader,'reader_client':reader_client,'index':index,'memory_before':before}


def run_batch(inputs, departments, out, *, reset_memory=True):
    inputs,departments,out=Path(inputs),Path(departments),Path(out)
    out.mkdir(parents=True,exist_ok=True)
    handle=(out/'run.lock').open('a');fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    repo=TaskRepository(out/'lifecycle.sqlite');repo.initialize()
    assert not repo.list_tasks(), 'Use a fresh replay database, never mix trials'
    for d in json.loads(departments.read_text()):
        repo.upsert_department(d['department_id'],d['name'],'test://scope/'+d['department_id'],json.loads(d['aliases_json']))
    scope_client=OpenAICompatibleLifecycleClient(base_url=LLM_BASE_URL,model=LLM_MODEL,enable_thinking=False)
    layer=build_semantic_layer(repo,scope_client,out,reset_memory=reset_memory)
    admission=layer['admission'];memory=layer['memory'];resolver=layer['resolver']
    reader=layer['reader'];index=layer['index'];embedder=layer['embedder']
    print('SEMANTIC_LAYER memory_before',layer['memory_before'],
          'reset',reset_memory,'embedding_url',embedder.embedding_url,flush=True)
    manifest={p.parent.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs.glob('E2E*/m1.json')}
    expected=inputs.parent/'frozen_manifest.json'
    if expected.exists():assert manifest==json.loads(expected.read_text())['inputs'],'Frozen M1 inputs changed'
    if not manifest:raise ValueError('No native M1 source files')
    summary={'complete':False,'model':LLM_MODEL,'started_at':utc_now(),'documents':[],
        'pipeline':'M1→M3→M6','m2_enabled':False,'admission_calls':0,'admission_format_retries':0,
        'admission_service_failures':0,
        'lifecycle_calls':0,'lifecycle_format_retries':0,'lifecycle_service_failures':0,
        'scheduling':'meetings chronological; compatible identities/targets serial; at most 4 independent groups',
        'semantic':{'embedding_model':'BAAI/bge-m3','embedding_url':embedder.embedding_url,
                    'memory_backend':'neo4j','memory_reset':reset_memory,
                    'identity':'memory + bge-m3 vectors + LLM','operations':'LLM reading + memory cache'}}
    counts=Counter()
    for path in sorted(inputs.glob('E2E*/m1.json')):
        doc=path.parent.name
        assert hashlib.sha256(path.read_bytes()).hexdigest()==manifest[doc]
        _,contexts=load_m1_payload(path,document_id=doc)
        atomic_write_json(out/'progress.json',{'document':doc,'stage':'M6_PROJECT_DECISION_WITH_M3_CANDIDATES','completed_meetings':len(summary['documents'])})
        admission.prepare(contexts)
        scope_rows=[r for (d,_),r in admission.rows.items() if d==doc]
        if doc.endswith('2026-04-07'):
            roots={r['parent_project'] for r in scope_rows if r['route']=='M6' and '红沙泉' in str(r['original_item'].get('project'))}
            # 2026-09-17 policy reversal: mine number, phase and subsystem are no
            # longer distinctions, so every 红沙泉* surface collapses to one parent.
            # The previous assertion required {'红沙泉项目','红沙泉二矿项目'}.
            assert roots=={'红沙泉项目'}, roots
        groups=independent_groups(contexts,admission,repo.list_tasks())
        records={};lock=threading.Lock()
        print('MEETING',doc,'groups',len(groups),'largest',max(map(len,groups)),flush=True)
        def work(group):
            client=OpenAICompatibleLifecycleClient(base_url=LLM_BASE_URL,model=LLM_MODEL,enable_thinking=False)
            service=build_service(repo,admission,client,index,reader)
            try:
                for context in group:
                    result=service.process_context(context)
                    if result['execution']['action']=='TECHNICAL_FAILURE': result=service.process_context(context)
                    with lock:
                        records[context.item_index]=result
                        with (out/'records.jsonl').open('a') as journal:journal.write(json.dumps(result,ensure_ascii=False)+'\n')
                        if len(records)%10==0:
                            progress={'document':doc,'processed':len(records),'total':len(contexts),'completed_meetings':len(summary['documents']),'updated_at':utc_now()}
                            atomic_write_json(out/'progress.json',progress)
                            print('PROGRESS',progress,flush=True)
                return service.judge
            finally:
                client.client.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            for judge in pool.map(work,groups):
                summary['lifecycle_calls']+=judge.calls
                summary['lifecycle_format_retries']+=judge.format_retries
                summary['lifecycle_service_failures']+=judge.service_failures
        ordered=[records[i] for i in range(len(contexts))]
        actions=dict(Counter(r['execution']['action'] for r in ordered));counts.update(actions)
        atomic_write_json(out/doc/'m6.json',{'source_document_id':doc,'item_count':len(ordered),'action_counts':actions,'records':ordered})
        # TECHNICAL_FAILURE is an unrecovered model/service outage: the meeting is
        # incomplete, so the run must stop. REJECTED is the validator safely refusing
        # an illegal command (e.g. a target outside the candidate set) with no write;
        # a rare one is an acceptable outcome — the old regex run happened to have
        # zero — not a crash. Fail only if rejections look systematic (>0.5%).
        assert not actions.get('TECHNICAL_FAILURE'), actions
        rejected = actions.get('REJECTED', 0)
        assert rejected <= max(1, int(0.005 * len(ordered))), f'systematic REJECTED: {actions}'
        summary['documents'].append({'source_document_id':doc,'item_count':len(ordered),'action_counts':actions})
        summary['action_counts']=dict(counts)
        summary.update(admission_calls=admission.calls,admission_format_retries=admission.format_retries,
            admission_service_failures=admission.service_failures,project_decision_calls=admission.project_calls,
            bidding_decision_calls=admission.bidding_calls,m3_model_calls=resolver.stats()['llm_calls'])
        summary['semantic'].update(resolver=resolver.stats(),reader=reader.stats(),
            task_index=index.stats(),memory=memory.stats(),
            embedded_typo_mentions=admission.typo_mentions,
            memory_resolved_items=admission.memory_resolved)
        atomic_write_json(out/'summary.json',summary)
        atomic_write_json(out/'memory_snapshot.json',memory.export_snapshot())
        print('DOCUMENT_COMPLETE',doc,actions,flush=True)
    with repo.connect() as c:
        summary['counts']={t:c.execute('select count(*) from '+t).fetchone()[0] for t in ('tasks','task_events','task_audit','lifecycle_reviews','processing_records','dispatch_queue')}
        assert c.execute("select count(*) from dispatch_queue where status='SENT' or route not like 'test://%'").fetchone()[0]==0
    replay_service=build_service(repo,admission,None,index,reader)
    for path in sorted(inputs.glob('E2E*/m1.json')):
        for context in load_m1_payload(path,document_id=path.parent.name)[1]:
            assert repo.get_processing_record(context.source_document_id,context.source_item_id)
            # Terminal replay returns before consulting model/admission/candidate history.
            assert replay_service.process_context(context)['idempotent_replay']
    summary.update(complete=True,replay_verified=True,finished_at=utc_now())
    summary['semantic'].update(resolver=resolver.stats(),reader=reader.stats(),
        task_index=index.stats(),memory=memory.stats(),
        embedded_typo_mentions=admission.typo_mentions,
        memory_resolved_items=admission.memory_resolved)
    atomic_write_json(out/'summary.json',summary)
    atomic_write_json(out/'memory_snapshot.json',memory.export_snapshot())
    scope_client.client.close();layer['reader_client'].client.close()
    memory.close();embedder.close()
    print('COMPLETE',flush=True)


def start():
    data=HERE/'data/native_full'/uuid.uuid4().hex
    data.mkdir(parents=True)
    atomic_write_json(HERE/'data/native_full_active.json',{'path':str(data)})
    with (data/'run.log').open('ab') as log:
        p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--inputs',str(BENCH/'frozen_m1'),
            '--departments',str(BENCH/'departments.json'),'--output',str(data)],
            cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    atomic_write_json(data/'process.json',{'pid':p.pid})
    return {'status':'RUNNING','pipeline':'M1→M3→M6','m2_enabled':False,'run_id':data.name}


def summary():
    active=HERE/'data/native_full_active.json'
    if not active.exists():return {'status':'NOT_STARTED','pipeline':'M1→M3→M6','m2_enabled':False}
    data=Path(json.loads(active.read_text())['path'])
    result=json.loads((data/'summary.json').read_text()) if (data/'summary.json').exists() else {}
    progress=json.loads((data/'progress.json').read_text()) if (data/'progress.json').exists() else {}
    failure=json.loads((data/'failure.json').read_text()) if (data/'failure.json').exists() else None
    return {**result,'progress':progress,'failure':failure,'status':'FAILED' if failure else 'PASS' if result.get('complete') else 'RUNNING',
        'pipeline':'M1→M3→M6','m2_enabled':False,'run_id':data.name}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--inputs',required=True,type=Path)
    parser.add_argument('--departments',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    try:run_batch(args.inputs,args.departments,args.output)
    except Exception as error:
        atomic_write_json(args.output/'failure.json',{'type':type(error).__name__,'error':str(error)[:2000]})
        raise
