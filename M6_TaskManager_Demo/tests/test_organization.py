import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.database import connect, initialize_database
from app.main import create_app
from app.organization import COMPANY, OFFICIAL_UNITS, USER_ADDED_UNITS, SHORT_NAMES, merge_existing_tasks, organization_key

from tests.helpers import OfflineReviewClient


class OrganizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = replace(Settings.load(), database_path=root/'tasks.sqlite', runs_dir=root/'runs', seed_demo_data=False)
        self.client = TestClient(create_app(settings, OfflineReviewClient()))
        self.repo = self.client.app.state.repository

    def tearDown(self):
        self.client.close(); self.temp.cleanup()

    def create(self, name):
        return self.repo.create_manual_task({'title': '测试任务', 'description': '原始要求', 'evidence_text': '真实来源片段', 'department': name})

    def test_directory_has_all_confirmed_units_even_without_tasks(self):
        directory = self.client.get('/api/organization').json()
        self.assertEqual(directory['company'], COMPANY)
        official = [unit for unit in directory['units'] if unit['kind'] == 'official']
        self.assertEqual([unit['name'] for unit in official], list(OFFICIAL_UNITS))
        self.assertEqual(len(official), 17)
        self.assertEqual(len(directory['units']), 17)
        self.assertTrue(all(unit['pending_count'] == 0 for unit in directory['units']))
        self.assertTrue(all(unit['parent_id'] == directory['organization_id'] for unit in directory['units']))

    def test_previously_retained_units_are_promoted_without_changing_tasks(self):
        task = self.create('工会')
        with connect(self.repo.database_path) as connection:
            connection.execute("UPDATE organization_units SET kind='retained' WHERE name='工会'")
            connection.commit()
        initialize_database(self.repo.database_path)
        unit = next(unit for unit in self.client.get('/api/organization').json()['units'] if unit['name']=='工会')
        self.assertEqual(unit['kind'], 'official')
        self.assertEqual(unit['department_id'], task['organization_id'])
        self.assertEqual(self.repo.get_task(task['task_id']), task)
        self.assertEqual(len(self.repo.list_events()), 1)

    def test_manual_writes_normalize_alias_and_audit_matches_stored_task(self):
        for short, name in SHORT_NAMES.items():
            task = self.create(short)
            self.assertEqual(task['department'], name)
            self.assertEqual(task['source_department'], short)
            self.assertEqual(task['organization_id'], organization_key(name))
            event = self.repo.list_events(task_id=task['task_id'])[0]
            self.assertEqual(event['after']['department'], name)
        updated = self.repo.update_task_by_human(task['task_id'], {'department': '北京分公司'})
        self.assertEqual(updated['department'], SHORT_NAMES['北京分公司'])
        self.assertEqual(updated['organization_id'], organization_key(SHORT_NAMES['北京分公司']))
        self.assertEqual(updated['source_department'], short)

    def test_old_entry_redirects_and_scope_is_shared(self):
        old = '北京分公司'; canonical = SHORT_NAMES[old]
        task = self.create(old)
        response = self.client.get('/departments/'+organization_key(old), follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers['location'], '/departments/'+organization_key(canonical))
        rows = self.client.get('/api/departments/'+organization_key(old)+'/tasks').json()
        self.assertEqual([row['task_id'] for row in rows], [task['task_id']])
        names = [unit['name'] for unit in self.client.get('/api/departments').json()]
        self.assertNotIn(old, names)
        self.assertEqual(names.count(canonical), 1)

    def test_legacy_merge_preserves_evidence_history_and_is_idempotent(self):
        with connect(self.repo.database_path) as connection:
            connection.execute('DROP TRIGGER tasks_organization_insert')
            connection.execute('DROP TRIGGER tasks_organization_update')
            connection.commit()
        legacy = self.create('人工智能研究院')
        history_before = self.repo.list_events(task_id=legacy['task_id'])[0]
        initialize_database(self.repo.database_path)
        result = merge_existing_tasks(self.repo.database_path)
        self.assertEqual(result, {'tasks_linked': 1, 'tasks_renamed': 1})
        after = self.repo.get_task(legacy['task_id'])
        self.assertEqual(after['department'], SHORT_NAMES['人工智能研究院'])
        self.assertEqual(after['source_department'], '人工智能研究院')
        self.assertEqual(after['version'], legacy['version']+1)
        for field in ('task_id', 'description', 'evidence_text', 'status', 'source_key', 'created_at'):
            self.assertEqual(after[field], legacy[field])
        events = self.repo.list_events(task_id=legacy['task_id'])
        self.assertEqual(next(event for event in events if event['event_id'] == history_before['event_id']), history_before)
        merge = next(event for event in events if event['db_action'] == 'ORG_MERGE')
        self.assertEqual(merge['before']['department'], '人工智能研究院')
        self.assertEqual(merge['after']['department'], after['department'])
        self.assertEqual(merge_existing_tasks(self.repo.database_path), {'tasks_linked': 0, 'tasks_renamed': 0})
        self.assertEqual(len(self.repo.list_events()), 2)

    def test_unknown_names_are_not_guessed_and_added_units_are_official(self):
        unknown = self.create('外部合作单位')
        self.assertEqual(unknown['department'], '外部合作单位')
        self.assertIsNone(unknown['organization_id'])
        for name in USER_ADDED_UNITS:
            task = self.create(name)
            self.assertEqual(task['department'], name)
        units = self.client.get('/api/organization').json()['units']
        self.assertEqual(next(unit['kind'] for unit in units if unit['name'] == '外部合作单位'), 'unmapped')

    def test_review_create_and_raw_updates_share_normalization(self):
        self.repo.enqueue_review_candidates('RUN', 'MEETING', [{'title': '复核任务', 'description': '真实来源片段',
            'evidence': '真实来源片段', 'department': '资产财务部'}])
        review = self.repo.list_review_candidates()[0]
        op = {'action': 'CREATE', 'target_task_id': None, 'expected_version': None, 'title': '复核任务',
              'description': '真实来源片段', 'department': '资产财务部', 'project': '', 'assignee_raw': '',
              'deadline_raw': '', 'priority': '', 'status': 'open', 'source_fragments': ['真实来源片段']}
        result = self.client.post('/api/review-candidates/'+review['candidate_id']+'/approve', json={'operations': [op]})
        self.assertEqual(result.status_code, 200, result.text)
        task_id = result.json()['results'][0]['task_id']
        self.assertEqual(self.repo.get_task(task_id)['department'], SHORT_NAMES['资产财务部'])
        with connect(self.repo.database_path) as connection:
            connection.execute('PRAGMA recursive_triggers=ON')
            connection.execute('UPDATE tasks SET department=? WHERE task_id=?', ('综合办公室', task_id))
            connection.commit()
        self.assertEqual(self.repo.get_task(task_id)['department'], SHORT_NAMES['综合办公室'])


if __name__ == '__main__':
    unittest.main()
