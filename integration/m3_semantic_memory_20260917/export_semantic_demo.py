"""Export the 11 completed meetings of the semantic M1→M3→M6 run to the Demo schema.

Adapted from integration/m1_m3_m6_20260915/export_twelve.py. Differences:

* The full run crashed on a strict ``assert not REJECTED`` at meeting 12 (0727),
  so ``summary['complete']`` is False. We export the 11 cleanly completed
  meetings listed in ``summary['documents']`` and drop everything from 0727.
* Task state is reconstructed from the last in-scope audit's ``after_state_json``
  (not the ``tasks`` table, whose current state already includes 0727 updates),
  so the exported snapshot is exactly "as of the 11th meeting".
* Names / m1 counts come from the existing verification.json (15 docs).

Read-only on the run; refuses to overwrite an existing Demo export.
"""
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path('/home/yty-s/meeting-m2-work')
DEMO = ROOT / 'M6_TaskManager_Demo'
SOURCE = Path(os.getenv('EXPORT_SOURCE') or
              ROOT / 'integration/m3_semantic_memory_20260917/runs/full_semantic')
TARGET = Path(os.getenv('EXPORT_TARGET') or
              DEMO / 'data/m1_m3_m6_semantic_20260917.sqlite')
VERIFICATION = ROOT / 'integration/m6_service/data/full_dataset_item_policy_v2_20260914/verification.json'
sys.path.insert(0, str(DEMO))
from app.database import initialize_database  # noqa: E402
from app.task_repository import TaskRepository  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent / 'candidate'))
from M6_TaskManager.src.executor import description_with_progress  # noqa: E402

STATUS = {'OPEN': 'open', 'IN_PROGRESS': 'in_progress', 'BLOCKED': 'blocked',
          'COMPLETED': 'completed', 'CANCELLED': 'cancelled', 'CLOSED': 'completed'}
ACTION = {'CREATE': 'CREATE', 'PROGRESS_UPDATE': 'UPDATE_FIELDS', 'MODIFY': 'UPDATE_FIELDS',
          'COMPLETE': 'UPDATE_STATUS', 'CANCEL': 'UPDATE_STATUS', 'REOPEN': 'UPDATE_STATUS',
          'TRANSFER': 'UPDATE_FIELDS', 'REVIEW': 'NOOP', 'SKIP': 'NOOP',
          'REJECTED': 'NOOP', 'TECHNICAL_FAILURE': 'NOOP', 'ROUTE_M4': 'ROUTE_M4',
          # task_audit never carries MULTI (its sub-commands carry real actions);
          # the entry is defensive so a future audit action cannot crash the export.
          'MULTI': 'NOOP'}


def meeting_id(doc):
    return 'M6RESULT-' + doc.removeprefix('E2E-FULL-')


def run_id(doc):
    return 'M6-SEM-20260917-' + doc.removeprefix('E2E-FULL-')


def snapshot(state, doc, item_id, evidence, content):
    if state is None:
        return None
    assignees = state.get('assignees')
    if assignees is None:
        assignees = json.loads(state.get('assignees_json', '[]'))
    return {'task_id': state['task_id'], 'title': state['title'],
            'description': state['description'], 'department': state.get('department'),
            'project': state.get('project'), 'project_id': state.get('project_entity_id'),
            'work_items': [content] if content else [],
            'assignee_raw': '、'.join(assignees) or None, 'deadline_raw': None, 'priority': None,
            'status': STATUS[state['status']], 'version': state['version'],
            'source_key': doc + ':' + item_id, 'source_meeting_id': meeting_id(doc),
            'source_segment_id': item_id, 'source_subsegment_id': 'M6',
            'evidence_text': evidence, 'is_deleted': 0,
            'created_at': state['created_at'], 'updated_at': state['updated_at']}


def evidence_of(provenance):
    trace = provenance.get('merge_trace', {})
    parts = [e.get('text', '') for e in trace.get('source_evidence', []) if e.get('text')]
    if parts:
        return '\n'.join(parts)
    return (provenance.get('item', {}).get('evidence') or {}).get('text', '')


def project_descriptions(audits, events):
    """Repair legacy display snapshots in memory; source audits stay read-only."""
    descriptions = {}
    for audit in audits:
        if not audit['after_state_json']:
            continue
        task_id = audit['task_id']
        before = json.loads(audit['before_state_json']) if audit['before_state_json'] else None
        after = json.loads(audit['after_state_json'])
        if before is not None:
            original = before['description']
            before['description'] = descriptions.get(task_id, original)
            if after['description'] == original:
                after['description'] = before['description']
            elif after['description'].startswith(original + '\n'):
                after['description'] = description_with_progress(
                    before['description'], after['description'][len(original):])
            audit['before_state_json'] = json.dumps(before, ensure_ascii=False)
        event = events.get((audit['source_document_id'], audit['source_item_id'], task_id))
        if event and ACTION[audit['action']] == 'UPDATE_FIELDS':
            after['description'] = description_with_progress(after['description'], event['content'])
        descriptions[task_id] = after['description']
        audit['after_state_json'] = json.dumps(after, ensure_ascii=False)


def main():
    summary = json.loads((SOURCE / 'summary.json').read_text())
    in_scope = [d['source_document_id'] for d in summary['documents']]
    in_scope_set = set(in_scope)
    counts_by_doc = {d['source_document_id']: d for d in summary['documents']}
    verification = json.loads(VERIFICATION.read_text())
    names = {d['source_document_id']: d['file_name'] for d in verification['documents']}
    m1_counts = {d['source_document_id']: d['m1_item_count'] for d in verification['documents']}

    source = sqlite3.connect('file:' + str(SOURCE / 'lifecycle.sqlite') + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    audits = [dict(r) for r in source.execute(
        'SELECT * FROM task_audit WHERE source_document_id IN (%s) ORDER BY rowid'
        % ','.join('?' for _ in in_scope), in_scope)]
    # MULTI items produce several events for the same (doc, item) — one per
    # sub-command — so the lookup key must include the task to avoid collapsing
    # them (and to keep demo event_ids unique, which is its PRIMARY KEY).
    events = {(r['source_document_id'], r['source_item_id'], r['task_id']): dict(r) for r in source.execute(
        'SELECT * FROM task_events WHERE source_document_id IN (%s)'
        % ','.join('?' for _ in in_scope), in_scope)}
    reviews = [dict(r) for r in source.execute(
        'SELECT * FROM lifecycle_reviews WHERE source_document_id IN (%s)'
        % ','.join('?' for _ in in_scope), in_scope)]

    project_descriptions(audits, events)
    by_task = defaultdict(list)
    for audit in audits:
        if audit['task_id']:
            by_task[audit['task_id']].append(audit)

    # Reconstruct each task's state as-of the last in-scope state-changing audit.
    task_state = {}
    for task_id, history in by_task.items():
        state = None
        for audit in history:  # chronological (ORDER BY rowid)
            if audit['after_state_json']:
                state = json.loads(audit['after_state_json'])
        if state is not None:
            task_state[task_id] = state

    temporary = TARGET.with_suffix('.building.sqlite')
    if TARGET.exists() or temporary.exists():
        raise SystemExit('Refusing to overwrite an existing Demo export: %s' % TARGET)
    initialize_database(temporary, seed_demo_data=False)
    repository = TaskRepository(temporary)

    for document_id in in_scope:
        date = document_id.removeprefix('E2E-FULL-')
        doc = counts_by_doc[document_id]
        repository.create_run(run_id(document_id), meeting_id(document_id), names[document_id], date,
            f'M1→M3→M6 语义记忆库（{len(in_scope)}场）', names[document_id].removesuffix('.pdf') + '（语义记忆库）',
            leader_requirements='【运行说明，非原文领导指令】语义向量+长期记忆库+LLM 判定项目归属；'
                                '红沙泉族统一归并、乌冬→乌东错字纠正、记忆规则复用。隔离测试，未实际派发。')
        applied = sum(n for a, n in doc['action_counts'].items() if a not in ('REVIEW', 'SKIP', 'ROUTE_M4', 'REJECTED', 'TECHNICAL_FAILURE'))
        repository.update_run(run_id(document_id), status='completed',
            m1_count=m1_counts[document_id], m2_count=0,
            command_count=doc['item_count'], applied_count=applied,
            noop_count=doc['action_counts'].get('SKIP', 0),
            summary_json=json.dumps(doc, ensure_ascii=False),
            finished_at=summary.get('finished_at') or audits[-1]['created_at'])

    target = sqlite3.connect(temporary)
    n_tasks = 0
    for task_id, state in task_state.items():
        history = by_task[task_id]
        first = history[0]
        last = history[-1]
        first_provenance = json.loads(first['provenance_json'])
        evidence = evidence_of(first_provenance)
        event = events.get((last['source_document_id'], last['source_item_id'], last['task_id']))
        content = event['content'] if event else first_provenance.get('item', {}).get('content', '')
        row = snapshot(state, first['source_document_id'], first['source_item_id'], evidence, content)
        if row is None:
            continue
        # MULTI: one item may CREATE several tasks; disambiguate the unique
        # source_key (doc:item) with the sub-command's goal index.
        if 'multi_goal_index' in first_provenance:
            row['source_key'] += '#g' + str(first_provenance['multi_goal_index'])
        row['work_items_json'] = json.dumps(row.pop('work_items'), ensure_ascii=False)
        keys = list(row)
        target.execute('INSERT INTO tasks (' + ','.join(keys) + ') VALUES (' + ','.join('?' for _ in keys) + ')',
                       [row[k] for k in keys])
        n_tasks += 1

    for audit in audits:
        doc, item_id = audit['source_document_id'], audit['source_item_id']
        provenance = json.loads(audit['provenance_json'])
        item = provenance.get('item', {})
        evidence = evidence_of(provenance)
        event = events.get((doc, item_id, audit['task_id']))
        content = event['content'] if event else item.get('content', '')
        before = snapshot(json.loads(audit['before_state_json']) if audit['before_state_json'] else None, doc, item_id, evidence, content)
        after = snapshot(json.loads(audit['after_state_json']) if audit['after_state_json'] else None, doc, item_id, evidence, content)
        target.execute('''INSERT INTO task_events(event_id,event_key,run_id,task_id,source_meeting_id,
            db_action,execution_status,before_json,after_json,evidence_text,failure_reason,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (
            event['event_id'] if event else audit['audit_id'], 'm6:' + audit['audit_id'], run_id(doc), audit['task_id'], meeting_id(doc),
            ACTION[audit['action']], ('routed' if audit['action'] == 'ROUTE_M4' else 'failed' if audit['action'] in ('REJECTED', 'TECHNICAL_FAILURE') else 'applied' if event else 'noop'),
            json.dumps(before, ensure_ascii=False) if before else None, json.dumps(after, ensure_ascii=False) if after else None,
            evidence, audit['action'] + '：' + audit['reason'] if not event else None, audit['created_at']))

    for review in reviews:
        item = json.loads(review['item_json'])
        candidate = {'title': review['source_document_id'].removeprefix('E2E-FULL-') + ' ' + item['title'],
            'description': item['content'], 'department': item.get('department'), 'project': item.get('project'),
            'assignee': '、'.join(item.get('assignee', [])), 'deadline': None, 'priority': None,
            'evidence': item['evidence']['text'], 'source_text': item['evidence']['text'],
            'source_item_id': review['source_item_id'], 'source_review_id': review['review_id'],
            'candidate_tasks': json.loads(review['candidates_json'])}
        target.execute('''INSERT INTO review_candidates(candidate_id,run_id,meeting_id,confidence_source,
            reason_code,candidate_json,status,created_at) VALUES (?,?,?,?,?,?,?,?)''', (
            review['review_id'], run_id(review['source_document_id']), meeting_id(review['source_document_id']),
            '语义记忆库+模型判定', review['reason'], json.dumps(candidate, ensure_ascii=False), 'pending', review['created_at']))

    target.commit()
    got_tasks = target.execute('SELECT count(*) FROM tasks').fetchone()[0]
    got_events = target.execute('SELECT count(*) FROM task_events').fetchone()[0]
    got_reviews = target.execute('SELECT count(*) FROM review_candidates').fetchone()[0]
    assert got_tasks == n_tasks, (got_tasks, n_tasks)
    assert got_events == len(audits), (got_events, len(audits))
    assert got_reviews == len(reviews), (got_reviews, len(reviews))
    assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    target.close()
    os.replace(temporary, TARGET)
    print(json.dumps({'database': str(TARGET), 'meetings': len(in_scope), 'tasks': n_tasks,
                      'events': len(audits), 'reviews': len(reviews)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
