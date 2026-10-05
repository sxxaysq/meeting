"""Audit actual execution history against scoped and pending-review restrictions."""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from full_dataset import DATA
from M6_TaskManager.src.m2_input import load_m2_payload
from M6_TaskManager.src.input_policy import restriction
from M6_TaskManager.src.candidate_retriever import normalize_text


def audit(path=DATA):
    path=Path(path)
    status=json.loads((path/'status.json').read_text())
    with sqlite3.connect(path/'m6/lifecycle.sqlite') as conn:
        conn.execute('BEGIN')
        records=conn.execute('SELECT rowid,source_document_id,source_item_id,action,task_id FROM processing_records ORDER BY rowid').fetchall()
        reviews={(doc,item):(json.loads(raw),reason,state) for doc,item,raw,reason,state in conn.execute(
            'SELECT source_document_id,source_item_id,item_json,reason,status FROM lifecycle_reviews')}
        review_ids={(doc,item):rid for doc,item,rid in conn.execute(
            'SELECT source_document_id,source_item_id,review_id FROM lifecycle_reviews')}
        reasons={(doc,item):reason for doc,item,reason in conn.execute('SELECT source_document_id,source_item_id,reason FROM task_audit')}
        counts={table:conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                for table in ('tasks','task_events','task_source_links','task_audit','lifecycle_reviews','processing_records')}
    contexts={}
    for doc in {r[1] for r in records}:
        _,loaded=load_m2_payload(path/'documents'/doc/'m2.json',document_id=doc,require_pass=False)
        contexts.update({(doc,s.source_item_id):s for s in loaded})
    pending=[]; violations=[]; dispositions=[]; review_reasons=Counter(); actions=Counter()
    for order,doc,item,action,task in records:
        source=contexts[(doc,item)]; reason=reasons.get((doc,item),''); actions[action]+=1
        project=normalize_text(source.item.get('project'))
        names={project} if project else set()
        for entity in source.project_entities:
            if entity['entity_id']==source.project_entity_id:
                names.update(normalize_text(n) for n in entity.get('aliases',[])+entity.get('source_names',[]) if n)
        if action not in ('REVIEW','SKIP'):
            if restriction(source,action): violations.append([doc,source.item_index,action,'CURRENT_ITEM_RISK'])
            for previous,why in pending:
                same=bool(names) and normalize_text(previous.get('project')) in names
                identity=any(k in why for k in ('PROJECT_ENTITY_UNCERTAIN','PROJECT_ALIAS_CONFLICT','PENDING_PROJECT_IDENTITY'))
                work=(not project and not previous.get('project')
                      and normalize_text(source.item.get('department'))==normalize_text(previous.get('department'))
                      and normalize_text(source.item['title'])==normalize_text(previous.get('title')))
                if same and identity:
                    violations.append([doc,source.item_index,action,'PENDING_IDENTITY'])
                elif action=='CREATE' and (same or work):
                    violations.append([doc,source.item_index,action,'PENDING_DUPLICATE'])
        if action=='REVIEW':
            prefix=reason.split(':',1)[0]
            known={'M2_ITEM_REVIEW','UNSCOPED_REVIEW','DUPLICATE_NOT_EXCLUDED','PENDING_PROJECT_IDENTITY',
                   'PENDING_REVIEW_DUPLICATE','DETERMINISTIC_SAFETY_REVIEW','LIFECYCLE_JUDGE_FAILURE','EXECUTION_CONFLICT'}
            review_reasons[prefix if prefix in known else 'MODEL_REVIEW']+=1
            previous,why,state=reviews[(doc,item)]
            if state=='PENDING': pending.append((previous,why))
        dispositions.append({'document':doc,'item_index':source.item_index,'title':source.item['title'],
                             'department':source.item.get('department'),'project':source.item.get('project'),
                             'action':action,'task_id':task,'review_id':review_ids.get((doc,item)),'reason':reason})
    result={'run_id':status['run_id'],'complete':status.get('test_execution')=='COMPLETED',
            'counts':counts,'action_counts':dict(actions),'review_reason_counts':dict(review_reasons),
            'checked_items':len(records),'policy_violations':violations}
    (path/'policy_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (path/'item_dispositions.json').write_text(json.dumps(dispositions,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    assert not violations, violations[:5]
    return result


if __name__=='__main__':
    audit(Path(sys.argv[1]) if len(sys.argv)>1 else DATA)
