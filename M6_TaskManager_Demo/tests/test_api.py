from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.database import connect
from app.main import create_app
from app.m6.executor import CommandExecutor
from app.m6.model_client import empty_patch
from tests.helpers import OfflineReviewClient


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        base = Settings.load(project_root=Path(__file__).resolve().parent.parent)
        settings = replace(
            base,
            database_path=root / "demo.db",
            runs_dir=root / "runs",
            static_dir=Path(__file__).resolve().parent.parent / "static",
            seed_demo_data=True,
        )
        self.client = TestClient(
            create_app(settings=settings, review_client=OfflineReviewClient())
        )

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_health_tasks_and_frontend(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")

        tasks = self.client.get("/api/tasks")
        self.assertEqual(tasks.status_code, 200)
        self.assertEqual(len(tasks.json()), 3)
        self.assertEqual(tasks.json()[0]["work_items"], [])

        detail = self.client.get("/api/tasks/T000001")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["task"]["task_id"], "T000001")

        frontend = self.client.get("/")
        self.assertEqual(frontend.status_code, 200)
        self.assertIn("煤矿会议智能任务管理", frontend.text)
        self.assertIn("工作环节", frontend.text)
        self.assertIn("查看历史任务", frontend.text)
        self.assertIn("人工复核", frontend.text)
        self.assertIn("每页", frontend.text)

    def test_task_history_returns_each_meeting_publication(self) -> None:
        repository = self.client.app.state.repository
        executor = CommandExecutor(repository.database_path)
        repository.create_run(
            run_id="RUN_HISTORY_1",
            meeting_id="MEETING_HISTORY_1",
            source_file="history-1.docx",
            meeting_date="2026-04-07",
            meeting_type="周例会",
            meeting_title="第一周工作安排",
        )
        created = executor.execute_command(
            source_key="MEETING_HISTORY_1:m1-001:001",
            command={
                "command_index": 1,
                "db_action": "CREATE",
                "target_task_id": None,
                "expected_version": None,
                "task_patch": empty_patch(
                    title="推进设备改造",
                    description="完成现场勘察。",
                    work_items=["现场勘察"],
                    status="open",
                ),
                "evidence": {
                    "segment_id": "m1-001",
                    "subsegment_id": "001",
                    "text": "第一周要求完成现场勘察。",
                },
            },
            run_id="RUN_HISTORY_1",
            meeting_id="MEETING_HISTORY_1",
        )
        task_id = created["task_id"]
        repository.create_run(
            run_id="RUN_HISTORY_2",
            meeting_id="MEETING_HISTORY_2",
            source_file="history-2.docx",
            meeting_date="2026-04-14",
            meeting_type="周例会",
            meeting_title="第二周工作安排",
        )
        executor.execute_command(
            source_key="MEETING_HISTORY_2:m1-002:001",
            command={
                "command_index": 1,
                "db_action": "UPDATE_FIELDS",
                "target_task_id": task_id,
                "expected_version": 1,
                "task_patch": empty_patch(
                    title="推进设备改造",
                    description="完成方案评审并准备施工。",
                    work_items=["方案评审", "施工准备"],
                ),
                "evidence": {
                    "segment_id": "m1-002",
                    "subsegment_id": "001",
                    "text": "第二周要求完成方案评审并准备施工。",
                },
            },
            run_id="RUN_HISTORY_2",
            meeting_id="MEETING_HISTORY_2",
        )

        response = self.client.get(f"/api/tasks/{task_id}/history")
        self.assertEqual(response.status_code, 200)
        history = response.json()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["published_at"], "2026-04-14")
        self.assertEqual(history[0]["work_items"], ["方案评审", "施工准备"])
        self.assertEqual(
            history[0]["evidence_text"],
            "第二周要求完成方案评审并准备施工。",
        )
        self.assertEqual(history[0]["meeting"]["meeting_title"], "第二周工作安排")
        self.assertTrue(history[0]["is_latest_release"])
        self.assertEqual(history[1]["description"], "完成现场勘察。")
        self.assertFalse(history[1]["is_latest_release"])

    def test_upload_is_unavailable_and_does_not_create_runs(self) -> None:
        for filename, media_type in (
            ("meeting.pdf", "application/pdf"),
            (
                "meeting.docx",
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document",
            ),
            ("meeting.txt", "text/plain"),
        ):
            response = self.client.post(
                "/api/demo/runs",
                data={"meeting_date": "2026-10-06", "meeting_type": "调度会"},
                files={"file": (filename, b"test document", media_type)},
            )
            self.assertEqual(response.status_code, 503)
            self.assertIn("M1→M3→M6", response.json()["detail"])
        self.assertEqual(self.client.app.state.repository.list_meetings(), [])
        self.assertEqual(
            list(self.client.app.state.settings.runs_dir.iterdir()), []
        )
        self.assertFalse(hasattr(self.client.app.state, "orchestrator"))

    def test_saved_run_queries_hide_retired_processing_counters(self) -> None:
        repository = self.client.app.state.repository
        repository.create_run(
            run_id="SEMANTIC_RUN",
            meeting_id="SEMANTIC_MEETING",
            source_file="result.docx",
            meeting_date="2026-10-06",
            meeting_type="调度会",
        )
        repository.update_run(
            "SEMANTIC_RUN",
            status="completed",
            m2_count=0,
            summary_json=json.dumps({"m2_count": 0, "applied_count": 3}),
        )
        run = self.client.get("/api/demo/runs/SEMANTIC_RUN")
        summary = self.client.get("/api/demo/runs/SEMANTIC_RUN/summary")
        self.assertEqual(run.status_code, 200)
        self.assertEqual(summary.status_code, 200)
        self.assertNotIn("m2_count", run.json())
        self.assertNotIn("m2_count", run.json()["summary"])
        self.assertNotIn("m2_count", summary.json())
        self.assertEqual(summary.json()["summary"], {"applied_count": 3})

    def test_human_review_candidate_can_create_a_task(self) -> None:
        repository = self.client.app.state.repository
        repository.enqueue_review_candidates(
            run_id="RUN_REVIEW",
            meeting_id="MEETING_REVIEW",
            candidates=[
                {
                    "candidate_id": "m1-001",
                    "title": "",
                    "description": "安排后续跟进",
                    "work_items": [],
                    "assignee": "信息部",
                    "deadline": "本周",
                    "evidence": "请信息部本周完成系统巡检。",
                    "confidence": 0.62,
                    "confidence_source": "model",
                    "reason": "low_model_confidence",
                }
            ],
        )
        candidates = self.client.get("/api/review-candidates")
        self.assertEqual(candidates.status_code, 200)
        self.assertEqual(len(candidates.json()), 1)
        candidate_id = candidates.json()[0]["candidate_id"]
        revised = self.client.patch(
            f"/api/review-candidates/{candidate_id}",
            json={"department": "信息部", "project": "系统运维", "initial_status": "in_progress"},
        )
        self.assertEqual(revised.status_code, 200)
        self.assertEqual(revised.json()["candidate"]["department"], "信息部")

        approved = self.client.post(
            f"/api/review-candidates/{candidate_id}/approve",
            json={"title": "完成系统巡检", "reviewer_note": "人工确认"},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["execution_status"], "applied")
        self.assertEqual(len(self.client.get("/api/tasks").json()), 4)
        task_id = approved.json()["task_id"]
        task = self.client.get(f"/api/tasks/{task_id}").json()["task"]
        self.assertEqual(task["department"], "信息部")
        self.assertEqual(task["project"], "系统运维")
        self.assertEqual(self.client.get("/api/review-candidates").json(), [])

    def test_manual_task_crud_and_training_export_keep_evidence(self) -> None:
        evidence = "调度会上明确要求机电队在完成设备检查后，于本周五前提交完整的整改方案和检查记录。" * 3
        created = self.client.post(
            "/api/tasks",
            json={
                "title": "提交设备整改方案",
                "description": "完成检查后报送整改材料。",
                "department": "机电队",
                "project": "设备安全",
                "priority": "高",
                "assignee_raw": "张三",
                "status": "open",
                "evidence_text": evidence,
            },
        )
        self.assertEqual(created.status_code, 201)
        task_id = created.json()["task_id"]
        updated = self.client.patch(
            f"/api/tasks/{task_id}",
            json={"department": "机电管理部", "status": "in_progress"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["department"], "机电管理部")
        self.assertEqual(updated.json()["evidence_text"], evidence)

        events = self.client.get("/api/events")
        self.assertEqual(events.status_code, 200)
        task_events = [event for event in events.json() if event["task_id"] == task_id]
        self.assertCountEqual(
            [event["db_action"] for event in task_events],
            ["HUMAN_UPDATE", "HUMAN_CREATE"],
        )

        exported = self.client.get("/api/training-samples/export")
        self.assertEqual(exported.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(exported.content)) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            self.assertEqual(manifest["schema_version"], "meeting_task_training_export_v1")
            sample = next(item for item in manifest["samples"] if item["sample_id"] == task_id)
            self.assertEqual(bundle.read(sample["evidence_file"]).decode("utf-8"), evidence)
            label = json.loads(bundle.read(sample["label_file"]))
            self.assertEqual(label["label"]["department"], "机电管理部")
            self.assertFalse(any(name.endswith("/context.json") for name in bundle.namelist()))

        deleted = self.client.delete(f"/api/tasks/{task_id}")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["is_deleted"])

    def test_training_export_embeds_same_meeting_context_in_evidence(self) -> None:
        repository = self.client.app.state.repository
        repository.create_run(
            run_id="RUN_TRAINING_CONTEXT",
            meeting_id="MEETING_TRAINING_CONTEXT",
            source_file="context.docx",
            meeting_date="2026-07-29",
            meeting_type="调度会",
            meeting_title="上下文导出测试",
        )
        evidence = "会议明确要求责任部门完成现场检查、形成整改记录并在规定时间前反馈执行结果。" * 3
        created_ids = []
        for title in ("完成现场检查", "提交整改记录", "反馈执行结果"):
            response = self.client.post(
                "/api/tasks",
                json={
                    "title": title,
                    "evidence_text": evidence + title,
                    "source_meeting_id": "MEETING_TRAINING_CONTEXT",
                },
            )
            self.assertEqual(response.status_code, 201)
            created_ids.append(response.json()["task_id"])

        with connect(repository.database_path) as connection:
            for index, task_id in enumerate(created_ids, start=1):
                connection.execute(
                    "UPDATE tasks SET source_segment_id = ? WHERE task_id = ?",
                    (f"m1-{index:03d}", task_id),
                )
            connection.commit()

        exported = self.client.get("/api/training-samples/export")
        self.assertEqual(exported.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(exported.content)) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            sample = next(item for item in manifest["samples"] if item["sample_id"] == created_ids[1])
            input_text = bundle.read(sample["evidence_file"]).decode("utf-8")
            self.assertIn("【会议信息】", input_text)
            self.assertIn("【目标原文证据】", input_text)
            self.assertIn("完成现场检查", input_text)
            self.assertIn("提交整改记录", input_text)
            self.assertIn("反馈执行结果", input_text)
            self.assertGreater(sample["evidence_chars"], len(evidence + "提交整改记录"))

    def test_task_list_can_be_limited_to_the_selected_meeting(self) -> None:
        repository = self.client.app.state.repository
        repository.create_run(
            run_id="RUN_MEETING_FILTER",
            meeting_id="MEETING_FILTER",
            source_file="filter.docx",
            meeting_date="2026-07-29",
            meeting_type="调度会",
        )
        created = self.client.post(
            "/api/tasks",
            json={
                "title": "仅属于筛选会议的任务",
                "evidence_text": "会议中明确安排责任人完成后续检查，并要求形成完整书面材料用于归档。" * 3,
                "source_meeting_id": "MEETING_FILTER",
            },
        )
        self.assertEqual(created.status_code, 201)
        filtered = self.client.get("/api/tasks?meeting_id=MEETING_FILTER")
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual([task["task_id"] for task in filtered.json()], [created.json()["task_id"]])


if __name__ == "__main__":
    unittest.main()
