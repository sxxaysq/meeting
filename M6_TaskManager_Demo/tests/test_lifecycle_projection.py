import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.database import initialize_database
from app.task_repository import TaskRepository


class LifecycleProjectionTest(unittest.TestCase):
    def test_new_m6_data_replaces_legacy_pipeline_rows_for_original_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "frontend.db"
            lifecycle = root / "lifecycle.db"
            initialize_database(frontend, seed_demo_data=True)
            repository = TaskRepository(frontend)
            for suffix, meeting_id in (("1", "MEETING-0407"), ("2", "MEETING-0413")):
                repository.create_run(
                    f"RUN-{suffix}", meeting_id, f"{suffix}.pdf",
                    f"2026-04-{suffix.zfill(2)}", "周例会",
                )

            schema = (
                Path(__file__).resolve().parents[2]
                / "M6_TaskManager" / "db" / "schema.sql"
            ).read_text(encoding="utf-8")
            with closing(sqlite3.connect(lifecycle)) as connection:
                connection.executescript(schema)
                connection.execute(
                    "INSERT INTO departments VALUES "
                    "('D1', '信息部', 'info', '[]', 1, 't1', 't1')"
                )
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "TASK-1", "PROJECT_TASK", "P1", "测试项目", "D1", "信息部",
                        None, None, "完成接口联调", "完成接口联调并提交结果",
                        json.dumps(["张三"], ensure_ascii=False), "IN_PROGRESS", 2,
                        "t1", "t2",
                    ),
                )
                for event_id, event_type, meeting_id, source_id, created_at in (
                    ("E1", "CREATE", "MEETING-0407", "I1", "t1"),
                    ("E2", "PROGRESS_UPDATE", "MEETING-0413", "I2", "t2"),
                ):
                    connection.execute(
                        "INSERT INTO task_events VALUES (?, 'TASK-1', ?, ?, ?, ?, ?)",
                        (event_id, event_type, "接口联调进展", meeting_id, source_id, created_at),
                    )
                    connection.execute(
                        "INSERT INTO task_source_links VALUES (?, 'TASK-1', ?, ?, ?, ?, '{}', ?)",
                        (
                            f"L-{event_id}", event_id, meeting_id, source_id,
                            json.dumps({"text": f"{meeting_id} 原文"}, ensure_ascii=False),
                            created_at,
                        ),
                    )
                connection.execute(
                    "INSERT INTO lifecycle_reviews VALUES "
                    "('REV-1', 'MEETING-0413', 'I3', ?, '[]', 'needs_review', "
                    "'PENDING', 't2', NULL)",
                    (json.dumps({"title": "待确认任务", "content": "待确认内容"}, ensure_ascii=False),),
                )
                connection.commit()

            result = repository.replace_pipeline_view_from_lifecycle(lifecycle)

            self.assertEqual(result, {"tasks": 1, "events": 2, "reviews": 1})
            self.assertEqual([task["task_id"] for task in repository.list_tasks()], ["TASK-1"])
            task = repository.get_task("TASK-1")
            self.assertEqual(task["status"], "in_progress")
            self.assertEqual(task["assignee_raw"], "张三")
            self.assertEqual(task["evidence_text"], "MEETING-0413 原文")
            self.assertEqual(len(repository.list_tasks(meeting_id="MEETING-0407")), 1)
            self.assertEqual(len(repository.list_tasks(meeting_id="MEETING-0413")), 1)
            self.assertEqual(len(repository.list_events(task_id="TASK-1")), 2)
            self.assertEqual(repository.list_review_candidates()[0]["candidate_id"], "REV-1")


if __name__ == "__main__":
    unittest.main()
