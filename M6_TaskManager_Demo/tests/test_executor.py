from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import initialize_database
from app.m6.executor import CommandExecutor
from app.m6.model_client import empty_patch
from app.task_repository import TaskRepository

from tests.helpers import command_batch


class CommandExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "demo.db"
        initialize_database(self.database_path, seed_demo_data=True)
        self.executor = CommandExecutor(self.database_path)
        self.repository = TaskRepository(self.database_path)
        self.meeting_id = "DEMO_EXEC"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_is_idempotent(self) -> None:
        source = {
            "segment_id": "seg_new",
            "subsegment_id": "001",
            "text": "通风队下周一前提交新风量测定方案。",
        }
        batch = command_batch(
            source,
            self.meeting_id,
            "CREATE",
            patch=empty_patch(
                title="提交新风量测定方案",
                description=source["text"],
                assignee_raw="通风队",
                deadline_raw="下周一前",
                status="open",
            ),
        )
        first = self.executor.execute_batch(
            batch, run_id="RUN1", meeting_id=self.meeting_id
        )
        second = self.executor.execute_batch(
            batch, run_id="RUN2", meeting_id=self.meeting_id
        )
        self.assertEqual(first[0]["execution_status"], "applied")
        self.assertEqual(second[0]["execution_status"], "duplicate")
        duplicate_events = self.repository.list_events(run_id="RUN2")
        self.assertEqual(len(duplicate_events), 1)
        self.assertEqual(
            duplicate_events[0]["execution_status"], "duplicate"
        )
        created = [
            task
            for task in self.repository.list_tasks()
            if task["title"] == "提交新风量测定方案"
        ]
        self.assertEqual(len(created), 1)

    def test_update_and_soft_delete_keep_history(self) -> None:
        source = {
            "segment_id": "seg_update",
            "subsegment_id": "001",
            "text": "原定周五提交3125设备应急方案延期到月底。",
        }
        update = command_batch(
            source,
            self.meeting_id,
            "UPDATE_FIELDS",
            patch=empty_patch(deadline_raw="月底"),
            target_task_id="T000001",
            expected_version=1,
        )
        updated = self.executor.execute_batch(
            update, run_id="RUN1", meeting_id=self.meeting_id
        )
        self.assertEqual(updated[0]["execution_status"], "applied")
        self.assertEqual(
            self.repository.get_task("T000001")["deadline_raw"], "月底"
        )

        delete_source = {
            "segment_id": "seg_delete",
            "subsegment_id": "001",
            "text": "T000001 这项任务取消，不再执行。",
        }
        delete = command_batch(
            delete_source,
            self.meeting_id,
            "SOFT_DELETE",
            target_task_id="T000001",
            expected_version=2,
        )
        deleted = self.executor.execute_batch(
            delete, run_id="RUN1", meeting_id=self.meeting_id
        )
        self.assertEqual(deleted[0]["execution_status"], "applied")
        task = self.repository.get_task("T000001")
        self.assertEqual(task["is_deleted"], 1)
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(len(self.repository.list_events(task_id="T000001")), 2)


if __name__ == "__main__":
    unittest.main()
