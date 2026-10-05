from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import initialize_database
from app.m6.executor import CommandExecutor
from app.m6.model_client import RuleBasedM6Client
from app.m6.service import TaskAutomationService, load_m2_records
from app.m6.target_matcher import TargetMatcher
from app.m6.validator import CommandValidator
from app.task_repository import TaskRepository

from tests.helpers import source_record


class TaskAutomationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "demo.db"
        initialize_database(self.database_path, seed_demo_data=True)
        self.repository = TaskRepository(self.database_path)
        self.service = TaskAutomationService(
            repository=self.repository,
            client=RuleBasedM6Client(),
            matcher=TargetMatcher(),
            validator=CommandValidator(),
            executor=CommandExecutor(self.database_path),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_four_database_actions(self) -> None:
        records = [
            source_record(
                "通风队下周一前提交新风量测定方案。",
                "task",
                "seg_new",
                speaker_id="通风队",
            ),
            source_record(
                "原定周五提交3125设备应急方案延期到月底。",
                "task_update",
                "seg_update",
            ),
            source_record(
                "完成主井设备检查这项任务已经全部完成。",
                "task_update",
                "seg_complete",
                speaker_id="运输队",
            ),
            source_record(
                "整理上月培训台账这项任务取消，不再执行。",
                "task_update",
                "seg_delete",
                speaker_id="培训科",
            ),
            source_record(
                "本月原煤产量完成计划的95%。",
                "report",
                "seg_report",
            ),
        ]
        summary = self.service.process_records(
            records,
            run_id="RUN_SERVICE",
            meeting={
                "meeting_id": "DEMO_SERVICE",
                "meeting_date": "2026-07-27",
                "meeting_type": "调度会",
            },
            output_dir=self.root / "m6",
        )
        self.assertEqual(summary["applied_count"], 4)
        self.assertEqual(summary["command_count"], 4)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["action_counts"]["CREATE"], 1)
        self.assertEqual(summary["action_counts"]["UPDATE_FIELDS"], 1)
        self.assertEqual(summary["action_counts"]["UPDATE_STATUS"], 1)
        self.assertEqual(summary["action_counts"]["SOFT_DELETE"], 1)
        self.assertEqual(
            self.repository.get_task("T000002")["status"], "completed"
        )
        self.assertEqual(
            self.repository.get_task("T000003")["is_deleted"], 1
        )

    def test_stale_m2_contract_is_rejected(self) -> None:
        record = source_record("机电队提交方案。", "task")
        record.pop("speaker_confidence")
        path = self.root / "stale.json"
        import json

        path.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "11 字段契约"):
            load_m2_records(path)

    def test_model_failure_is_audited_and_does_not_modify_tasks(self) -> None:
        class BrokenClient:
            def generate(self, *args, **kwargs):
                raise RuntimeError("simulated model outage")

        service = TaskAutomationService(
            repository=self.repository,
            client=BrokenClient(),
            matcher=TargetMatcher(),
            validator=CommandValidator(),
            executor=CommandExecutor(self.database_path),
        )
        summary = service.process_records(
            [source_record("通风队提交新方案。", "task", "seg_fail")],
            run_id="RUN_FAILURE",
            meeting={
                "meeting_id": "DEMO_FAILURE",
                "meeting_date": "2026-07-27",
                "meeting_type": "调度会",
            },
            output_dir=self.root / "m6_failure",
        )
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["command_count"], 1)
        events = self.repository.list_events(run_id="RUN_FAILURE")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["execution_status"], "failed")
        self.assertEqual(events[0]["db_action"], "M6_MODEL_FAILURE")

    def test_legacy_multi_create_is_merged_before_database_write(self) -> None:
        class LegacyOverSplitClient:
            def generate(self, meeting, source, match_context, correction=None):
                del match_context, correction
                commands = []
                for index, item in enumerate(
                    ("设计灾害预警原型", "完成功能测试及 bug 修复"),
                    start=1,
                ):
                    commands.append(
                        {
                            "command_index": index,
                            "db_action": "CREATE",
                            "target_task_id": None,
                            "expected_version": None,
                            "task_patch": {
                                "title": f"智能综合管控平台：{item}",
                                "description": item,
                                "assignee_raw": "新疆交付组",
                                "deadline_raw": None,
                                "status": "in_progress",
                            },
                            "evidence": {
                                "segment_id": source["segment_id"],
                                "subsegment_id": source["subsegment_id"],
                                "text": source["text"],
                            },
                            "reason_code": "NEW_TASK",
                            "ambiguities": [],
                        }
                    )
                return (
                    {
                        "schema_version": "m6_db_command_v1",
                        "source_key": (
                            f"{meeting['meeting_id']}:{source['segment_id']}:"
                            f"{source['subsegment_id']}"
                        ),
                        "commands": commands,
                    },
                    {"model": "legacy-test-client", "usage": {}},
                )

        service = TaskAutomationService(
            repository=self.repository,
            client=LegacyOverSplitClient(),
            matcher=TargetMatcher(),
            validator=CommandValidator(),
            executor=CommandExecutor(self.database_path),
        )
        summary = service.process_records(
            [
                source_record(
                    "④智能综合管控平台：设计灾害预警原型；"
                    "完成功能测试及 bug 修复。",
                    "task",
                    "seg_granularity",
                    "011",
                    "新疆交付组",
                )
            ],
            run_id="RUN_GRANULARITY",
            meeting={
                "meeting_id": "DEMO_GRANULARITY",
                "meeting_date": "2026-07-27",
                "meeting_type": "调度会",
            },
            output_dir=self.root / "m6_granularity",
        )
        self.assertEqual(summary["applied_count"], 1)
        self.assertEqual(summary["command_count"], 1)
        created = [
            task
            for task in self.repository.list_tasks()
            if task["source_meeting_id"] == "DEMO_GRANULARITY"
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["title"], "构建智能综合管控平台")
        self.assertEqual(len(created[0]["work_items"]), 2)


if __name__ == "__main__":
    unittest.main()
