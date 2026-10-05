import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.department_workbench import department_key
from app.main import create_app
from app.m6.model_client import RuleBasedM6Client


class DepartmentWorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = replace(Settings.load(), database_path=root/'tasks.sqlite', runs_dir=root/'runs', seed_demo_data=False)
        self.client = TestClient(create_app(settings, RuleBasedM6Client()))
        self.repo = self.client.app.state.repository
        for meeting in ('A', 'B'):
            self.repo.create_run('RUN'+meeting, 'M'+meeting, meeting+'.pdf', '2026-09-20', '周例会',
                                 meeting+'会议', leader_requirements='会议内部全部部门指令')
        self.own = [self.task('部门A', status) for status in ('open', 'in_progress', 'blocked')]
        self.closed = self.task('部门A', 'completed')
        self.cancelled = self.task('部门A', 'cancelled')
        deleted = self.task('部门A', 'open')
        self.repo.soft_delete_task_by_human(deleted['task_id'])
        self.other = self.task('部门B', 'open')
        self.similar = self.task('部门A（中心）', 'open')
        self.task(None, 'open')
        self.base = '/api/departments/' + department_key('部门A')

    def task(self, department, status):
        return self.repo.create_manual_task({'title': '任务'+str(department), 'description': '原始要求',
            'department': department, 'status': status, 'evidence_text': '原文证据',
            'source_meeting_id': 'MA' if department == '部门A' else 'MB'})

    def tearDown(self):
        self.client.close(); self.temp.cleanup()

    def test_department_directory_and_pending_filter(self):
        departments = self.client.get('/api/departments').json()
        own = next(item for item in departments if item['name'] == '部门A')
        self.assertEqual(own['pending_count'], 3)
        self.assertEqual(own['blocked_count'], 1)
        self.assertEqual(self.client.get(own['url']).status_code, 200)
        rows = self.client.get(self.base+'/tasks?include_deleted=true&department=部门B').json()
        self.assertEqual({task['task_id'] for task in rows}, {task['task_id'] for task in self.own})
        self.assertEqual(len(self.client.get(self.base+'/tasks?status=blocked').json()), 1)
        self.assertEqual(self.client.get(self.base+'/tasks?status=completed').status_code, 400)
        self.assertEqual(self.client.get('/api/departments/unknown/tasks').status_code, 404)
        self.assertEqual(len(self.client.get('/api/tasks').json()), 8)  # Admin remains unchanged.

    def test_foreign_detail_history_write_and_admin_fields_are_denied(self):
        for foreign in (self.other, self.similar):
            url = self.base+'/tasks/'+foreign['task_id']
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.get(url+'/history').status_code, 404)
            result = self.client.patch(url, json={'status': 'completed', 'expected_version': foreign['version']})
            self.assertEqual(result.status_code, 404)
            self.assertEqual(self.repo.get_task(foreign['task_id'])['status'], 'open')
        url = self.base+'/tasks/'+self.own[0]['task_id']
        self.assertEqual(self.client.delete(url).status_code, 405)
        self.assertEqual(self.client.patch(url, json={'status': 'open', 'expected_version': 1, 'department': '部门B'}).status_code, 400)
        self.assertEqual(self.client.patch(url, json={'status': 'cancelled', 'expected_version': 1}).status_code, 400)

    def test_progress_audit_completion_and_stale_version(self):
        task = self.own[0]; url = self.base+'/tasks/'+task['task_id']
        result = self.client.patch(url, json={'status': 'in_progress', 'progress_note': '已完成现场检查', 'expected_version': 1})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['description'], '原始要求\n已完成现场检查')
        self.assertEqual(result.json()['department'], '部门A')
        self.assertEqual(self.client.patch(url, json={'status': 'completed', 'expected_version': 1}).status_code, 409)
        history = self.client.get(url+'/history').json()
        self.assertTrue(any(item['db_action'] == 'HUMAN_UPDATE' and '现场检查' in item['description'] for item in history))
        result = self.client.patch(url, json={'status': 'completed', 'progress_note': '验收完成', 'expected_version': 2})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(task['task_id'], {row['task_id'] for row in self.client.get(self.base+'/tasks').json()})
        self.assertEqual(self.client.get('/api/tasks/'+task['task_id']).json()['task']['status'], 'completed')

    def test_reassignment_checked_again_at_write_boundary(self):
        task = self.own[0]
        self.repo.update_task_by_human(task['task_id'], {'department': '部门B'})
        response = self.client.patch(self.base+'/tasks/'+task['task_id'], json={'status': 'completed', 'expected_version': 1})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.repo.get_task(task['task_id'])['status'], 'open')

    def test_meetings_are_relevant_metadata_only(self):
        meetings = self.client.get(self.base+'/meetings').json()
        self.assertEqual({meeting['meeting_id'] for meeting in meetings}, {'MA'})
        self.assertNotIn('leader_requirements', meetings[0])
        detail = self.client.get(self.base+'/tasks/'+self.own[0]['task_id']).json()
        self.assertNotIn('leader_requirements', detail['meeting'])


if __name__ == '__main__':
    unittest.main()
