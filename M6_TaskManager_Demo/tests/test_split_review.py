import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.review_plan import FIELDS, suggest_operations

from tests.helpers import OfflineReviewClient


class SplitReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = replace(Settings.load(), database_path=root/'test.sqlite', runs_dir=root/'runs', seed_demo_data=False)
        self.client = TestClient(create_app(settings, OfflineReviewClient()))
        self.repo = self.client.app.state.repository
        self.tasks = [self.repo.create_manual_task({'title': title, 'description': title+'原要求',
            'department': '原部门', 'project': '原项目', 'evidence_text': title+'原文'}) for title in ('设备安装', '平台开发')]
        self.repo.enqueue_review_candidates('RUN', 'MEETING', [{'title': '设备与平台进展',
            'description': '设备安装完成；平台开发推进；编制培训方案。',
            'evidence': '设备安装完成；平台开发推进；编制培训方案。',
            'department': '来文部门', 'candidate_tasks': self.tasks}])
        self.review = self.repo.list_review_candidates()[0]
        self.url = '/api/review-candidates/' + self.review['candidate_id']

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def operations(self):
        operations = []
        for task, text in zip(self.tasks, ['设备安装完成', '平台开发推进']):
            op = {field: str(task.get(field) or '') for field in FIELDS}
            op.update(action='UPDATE_FIELDS', target_task_id=task['task_id'], expected_version=task['version'],
                      description=text, source_fragments=[text], reason='同一目标')
            operations.append(op)
        return operations

    def test_multiple_updates_and_create_are_audited_and_idempotent(self):
        ops = self.operations()
        new = {field: '' for field in FIELDS}
        new.update(action='CREATE', target_task_id=None, expected_version=None, title='培训方案',
                   description='编制培训方案。', status='open', source_fragments=['编制培训方案。'])
        ops.append(new)
        result = self.client.post(self.url+'/approve', json={'operations': ops})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json()['results']), 3)
        self.assertEqual(len(self.repo.list_tasks()), 3)
        for task, text in zip(self.tasks, ['设备安装完成', '平台开发推进']):
            current = self.repo.get_task(task['task_id'])
            self.assertEqual(current['description'], task['description']+'\n'+text)
            self.assertEqual(current['version'], task['version']+1)
            event = next(e for e in self.repo.list_events(task_id=task['task_id']) if e['db_action']=='UPDATE_FIELDS')
            self.assertEqual(event['source_meeting_id'], 'MEETING')
            self.assertEqual(event['evidence_text'], self.review['candidate']['evidence'])
            self.assertEqual(event['before']['description'], task['description'])
            self.assertEqual(event['after']['description'], current['description'])
        before_events = len(self.repo.list_events())
        repeated = self.client.post(self.url+'/approve', json={'operations': ops})
        self.assertEqual(repeated.json()['execution_status'], 'duplicate')
        self.assertEqual(len(self.repo.list_events()), before_events)
        self.assertEqual(self.repo.get_review_candidate(self.review['candidate_id'])['status'], 'approved')

    def test_second_target_version_conflict_rolls_back_everything(self):
        ops = self.operations()
        ops[1]['expected_version'] += 1
        before = self.repo.list_tasks()
        events = self.repo.list_events()
        result = self.client.post(self.url+'/approve', json={'operations': ops})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.repo.list_tasks(), before)
        self.assertEqual(self.repo.list_events(), events)
        self.assertEqual(self.repo.get_review_candidate(self.review['candidate_id'])['status'], 'pending')

    def test_save_plan_restores_all_children_without_executing_or_model_call(self):
        ops = self.operations()
        self.assertEqual(self.client.patch(self.url, json={'operations': ops}).status_code, 200)
        with patch('app.main.suggest_operations', side_effect=AssertionError('must reuse draft')):
            result = self.client.post(self.url+'/plan')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['operations'], ops)
        self.assertEqual([t['task_id'] for t in result.json()['tasks']], [t['task_id'] for t in self.tasks])
        self.assertEqual(len(self.repo.list_events()), 2)

    def test_bad_target_duplicate_target_and_forged_evidence_are_rejected(self):
        for field, value in [('target_task_id', 'UNKNOWN'), ('expected_version', True),
                             ('source_fragments', ['无来源的虚构片段']), ('description', [])]:
            ops = self.operations(); ops[1][field] = value
            result = self.client.post(self.url+'/approve', json={'operations': ops})
            self.assertEqual(result.status_code, 400, result.text)
        ops = self.operations(); ops[1] = copy.deepcopy(ops[0])
        self.assertEqual(self.client.post(self.url+'/approve', json={'operations': ops}).status_code, 400)
        self.assertEqual(len(self.repo.list_events()), 2)

    def test_model_plan_uses_current_target_defaults_and_source_fragments(self):
        proposed = {'operations': [
            {'action': 'UPDATE_FIELDS', 'target_index': 0, 'title': '不应覆盖原名', 'fragments': ['设备安装完成'], 'reason': '设备子目标'},
            {'action': 'UPDATE_FIELDS', 'target_index': 1, 'title': '不应覆盖原名', 'fragments': ['平台开发推进'], 'reason': '平台子目标'},
        ]}
        completion = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(proposed)))]))
        client = SimpleNamespace(model='test', client=SimpleNamespace(chat=SimpleNamespace(completions=completion)))
        ops = suggest_operations(self.review, {t['task_id']: t for t in self.tasks}, client)
        self.assertEqual([op['title'] for op in ops], ['设备安装', '平台开发'])
        self.assertEqual([op['department'] for op in ops], ['原部门', '原部门'])
        self.assertEqual([op['description'] for op in ops], ['设备安装完成', '平台开发推进'])
        proposed['operations'][0]['fragments'] = ['虚构内容']
        with self.assertRaises(ValueError):
            suggest_operations(self.review, {t['task_id']: t for t in self.tasks}, client)


if __name__ == '__main__':
    unittest.main()
