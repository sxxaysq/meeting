from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import connect, initialize_database, utc_now
from app.m6.executor import CommandExecutor
from app.m6.granularity import normalize_task_granularity
from app.m6.model_client import empty_patch
from app.m6.validator import CommandValidator
from app.task_repository import TaskRepository
from scripts.migrate_task_granularity import (
    CANONICAL_TASK_ID,
    SOURCE_TASK_IDS,
    TITLE,
    WORK_ITEMS,
    migrate,
)


class TaskGranularityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = {
            "segment_id": "seg_platform",
            "subsegment_id": "011",
            "actor_hint": "新疆交付组",
            "text": (
                "④智能综合管控平台：设计灾害预警系统原型、"
                "综合大屏可视化原型各 1 版；推进流程中心功能测试"
                "及bug修复、流程设计引擎研发等后续工作。\n"
            ),
            "task_category": "task",
        }

    def _batch(
        self,
        titles: list[str],
        assignees: list[str] | None = None,
    ) -> dict:
        assignees = assignees or ["新疆交付组"] * len(titles)
        commands = []
        for index, (title, assignee) in enumerate(
            zip(titles, assignees),
            start=1,
        ):
            item = title.split("：", 1)[-1]
            commands.append(
                {
                    "command_index": index,
                    "db_action": "CREATE",
                    "target_task_id": None,
                    "expected_version": None,
                    "task_patch": empty_patch(
                        title=title,
                        description=item,
                        assignee_raw=assignee,
                        status="in_progress",
                    ),
                    "evidence": {
                        "segment_id": self.source["segment_id"],
                        "subsegment_id": self.source["subsegment_id"],
                        "text": self.source["text"],
                    },
                    "reason_code": "NEW_TASK",
                    "ambiguities": [],
                }
            )
        return {
            "schema_version": "m6_db_command_v1",
            "source_key": "MEETING:seg_platform:011",
            "commands": commands,
        }

    def test_same_platform_steps_merge_into_one_task(self) -> None:
        titles = [
            "智能综合管控平台：设计灾害预警系统原型",
            "智能综合管控平台：设计综合大屏可视化原型",
            "智能综合管控平台：推进流程中心功能测试及bug修复",
            "智能综合管控平台：推进流程设计引擎研发",
        ]
        normalized, audit = normalize_task_granularity(
            self._batch(titles),
            self.source,
        )
        self.assertTrue(audit["applied"])
        self.assertEqual(len(normalized["commands"]), 1)
        patch = normalized["commands"][0]["task_patch"]
        self.assertEqual(patch["title"], "构建智能综合管控平台")
        self.assertEqual(len(patch["work_items"]), 4)
        CommandValidator().validate_batch(
            normalized,
            source=self.source,
            meeting_id="MEETING",
            match_context={"candidates": []},
        )

    def test_single_create_with_work_items_gets_canonical_title(self) -> None:
        batch = self._batch(
            ["智能综合管控平台原型设计与功能研发"]
        )
        batch["commands"][0]["task_patch"]["work_items"] = [
            "设计灾害预警系统原型",
            "推进流程中心功能测试",
        ]
        normalized, audit = normalize_task_granularity(batch, self.source)
        self.assertTrue(audit["applied"])
        self.assertEqual(len(normalized["commands"]), 1)
        self.assertEqual(
            normalized["commands"][0]["task_patch"]["title"],
            "构建智能综合管控平台",
        )
        self.assertEqual(
            normalized["commands"][0]["reason_code"],
            "CANONICALIZED_SHARED_BUSINESS_GOAL",
        )

    def test_two_independent_business_goals_stay_separate(self) -> None:
        source = dict(
            self.source,
            text="建设甲平台并完成乙系统验收。",
        )
        batch = self._batch(["建设甲平台", "完成乙系统验收"])
        for command in batch["commands"]:
            command["evidence"]["text"] = source["text"]
        normalized, audit = normalize_task_granularity(batch, source)
        self.assertFalse(audit["applied"])
        self.assertEqual(len(normalized["commands"]), 2)

    def test_different_assignees_do_not_merge(self) -> None:
        titles = [
            "智能综合管控平台：设计原型",
            "智能综合管控平台：完成测试",
        ]
        normalized, audit = normalize_task_granularity(
            self._batch(titles, ["新疆交付组", "软件技术组"]),
            self.source,
        )
        self.assertFalse(audit["applied"])
        self.assertEqual(len(normalized["commands"]), 2)

    def test_merged_create_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.db"
            initialize_database(database)
            titles = [
                "智能综合管控平台：设计原型",
                "智能综合管控平台：完成功能测试",
            ]
            batch, _ = normalize_task_granularity(
                self._batch(titles),
                self.source,
            )
            executor = CommandExecutor(database)
            first = executor.execute_batch(batch, "RUN1", "MEETING")
            second = executor.execute_batch(batch, "RUN2", "MEETING")
            self.assertEqual(first[0]["execution_status"], "applied")
            self.assertEqual(second[0]["execution_status"], "duplicate")
            tasks = TaskRepository(database).list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["title"], "构建智能综合管控平台")
            self.assertEqual(len(tasks[0]["work_items"]), 2)


class ExistingTaskMigrationTest(unittest.TestCase):
    def test_existing_four_tasks_are_audited_and_soft_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.db"
            initialize_database(database)
            evidence = (
                "④智能综合管控平台：设计灾害预警系统原型、"
                "综合大屏可视化原型各 1 版；推进流程中心功能测试"
                "及bug修复、流程设计引擎研发等后续工作。\n"
            )
            now = utc_now()
            with connect(database) as connection:
                for index, task_id in enumerate(SOURCE_TASK_IDS, start=1):
                    connection.execute(
                        """
                        INSERT INTO tasks (
                            task_id, title, description, assignee_raw,
                            deadline_raw, status, source_key,
                            source_meeting_id, source_segment_id,
                            source_subsegment_id, evidence_text,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, '新疆交付组', NULL, 'in_progress',
                                  ?, 'MEETING', 'seg', '011', ?, ?, ?)
                        """,
                        (
                            task_id,
                            f"智能综合管控平台：环节{index}",
                            f"环节{index}",
                            f"MEETING:seg:011:{index}",
                            evidence,
                            now,
                            now,
                        ),
                    )
                connection.commit()

            result = migrate(database)
            self.assertEqual(result["status"], "applied")
            repository = TaskRepository(database)
            active = repository.list_tasks()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["task_id"], CANONICAL_TASK_ID)
            self.assertEqual(active[0]["title"], TITLE)
            self.assertEqual(active[0]["work_items"], WORK_ITEMS)
            all_tasks = repository.list_tasks(include_deleted=True)
            self.assertEqual(sum(task["is_deleted"] for task in all_tasks), 3)
            events = [
                event
                for event in repository.list_events()
                if event["run_id"] == "MIGRATION_TASK_GRANULARITY_V1"
            ]
            self.assertEqual(len(events), 4)
            self.assertEqual(migrate(database)["status"], "already_applied")


if __name__ == "__main__":
    unittest.main()
