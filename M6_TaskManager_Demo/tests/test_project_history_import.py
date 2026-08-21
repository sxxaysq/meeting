from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.database import connect, initialize_database
from scripts.import_m2_task_candidates import import_candidates
from scripts.migrate_duplicate_task_history import migrate
from scripts.migrate_project_progress_groups import plan_migrations


PROJECT_SCHEMA = """
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE project_aliases (
    alias_normalized TEXT PRIMARY KEY,
    alias_name TEXT NOT NULL,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def candidate(
    candidate_id: str,
    title: str,
    description: str,
    work_items: list[str],
    evidence: str,
    *,
    department: str = "天地奔牛",
    assignee: str = "",
    status: str = "进行中",
    canonical_name: str = "ERP项目",
    project_id: str = "P_ERP",
    project_confidence: float = 0.99,
) -> dict:
    source_text = "\n".join([
        "部门：" + department,
        "项目：ERP项目",
        "任务：" + title,
        description,
        "证据：" + evidence,
        "工作项：" + "；".join(work_items),
    ])
    return {
        "candidate_id": candidate_id,
        "department": department,
        "project": "ERP项目",
        "title": title,
        "description": description,
        "work_items": work_items,
        "assignee": assignee,
        "deadline": "",
        "priority": "高",
        "status": status,
        "evidence": evidence,
        "confidence": 0.95,
        "route": "EXTRACT",
        "source_text": source_text,
        "project_context": {
            "project_id": project_id,
            "canonical_name": canonical_name,
            "source_name": canonical_name,
            "resolution": "existing",
            "confidence": project_confidence,
        },
    }


def payload(item: dict) -> dict:
    return {
        "schema_version": "m2.task_gate.v2",
        "source_file": "meeting.pdf",
        "extract_candidates": [item],
        "review_candidates": [],
    }


class ProjectHistoryImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "demo.db"
        initialize_database(self.database)
        with connect(self.database) as connection:
            connection.executescript(PROJECT_SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, canonical_name, created_at, updated_at
                ) VALUES ('P_ERP', 'ERP项目', '2026-04-01', '2026-04-01')
                """
            )
            connection.commit()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _import(self, item: dict, meeting_id: str, run_id: str) -> dict:
        return import_candidates(
            payload(item),
            self.database,
            self.root / (run_id + ".json"),
            meeting_id=meeting_id,
            run_id=run_id,
        )

    def test_cross_meeting_progress_updates_one_project_task(self) -> None:
        first = candidate(
            "m1-004",
            "天地奔牛ERP项目方案沟通与多模块上线运维",
            "推进财务成本方案沟通与确定，开展项目开发与人资上线运维。",
            [
                "财务成本方案沟通汇报",
                "项目开发工作",
                "成本方案确定",
                "人资上线运维",
            ],
            "财务成本方案沟通汇报；项目开发工作；成本方案确定；人资上线运维。",
            assignee="闫晓文",
        )
        second = candidate(
            "m1-003",
            "天地奔牛ERP项目开发与上线运维",
            "完成生产计划与成本方案签字，开展项目开发工作，负责人资与传动系统上线运维。",
            [
                "完成生产计划与成本方案签字",
                "开展项目开发工作",
                "运维人资系统上线",
                "运维传动系统上线",
            ],
            "生产计划方案签字；项目开发工作；成本方案签字；人资上线运维；传动上线运维。",
        )
        third = candidate(
            "m1-017",
            "天地奔牛ERP项目开发上线运维推进",
            "汇报生产计划方案，开展项目开发，确定成本方案，推进人资/传动上线运维及链条上线准备。",
            [
                "生产计划部分方案汇报",
                "开展项目开发工作",
                "成本方案确定",
                "人资上线运维",
                "传动上线运维",
                "链条上线准备",
            ],
            "生产计划部分方案给闫总汇报；项目开发工作；成本方案确定；人资上线运维；传动上线运维；链条上线准备。",
        )
        fourth = candidate(
            "m1-005",
            "天地奔牛ERP系统开发与多模块上线运维",
            "开发生产计划与财务成本模块，推进人资、传动、链条模块上线运维，完成BOM与工艺数据整理迁移。",
            [
                "生产计划部门系统开发",
                "财务成本需求开发",
                "人资上线运维",
                "传动与链条上线运维",
                "BOM与工艺数据整理迁移",
                "奔牛上线数据整理",
            ],
            "ERP项目：生产计划部门系统开发；财务成本需求开发；人资上线运维；传动上线运维；链条上线运维；BOM与工艺数据整理迁移。",
            department="数字化技术服务事业部",
            assignee="闫晓文",
        )

        first_report = self._import(first, "MEETING_1", "RUN_1")
        second_report = self._import(second, "MEETING_2", "RUN_2")
        third_report = self._import(third, "MEETING_3", "RUN_3")
        fourth_report = self._import(fourth, "MEETING_4", "RUN_4")

        self.assertEqual(first_report["results"][0]["db_action"], "CREATE")
        for report in (second_report, third_report, fourth_report):
            self.assertEqual(
                report["results"][0]["db_action"],
                "UPDATE_FIELDS",
                report["match_decisions"][0],
            )
            self.assertEqual(
                report["match_decisions"][0]["decision"],
                "UPDATE",
            )
        with connect(self.database) as connection:
            tasks = connection.execute(
                """
                SELECT * FROM tasks
                WHERE project_id = 'P_ERP' AND is_deleted = 0
                """
            ).fetchall()
            events = connection.execute(
                """
                SELECT db_action FROM task_events
                WHERE execution_status = 'applied'
                ORDER BY rowid
                """
            ).fetchall()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["version"], 4)
        self.assertEqual(tasks[0]["title"], fourth["title"])
        self.assertEqual(
            [row["db_action"] for row in events],
            [
                "CREATE",
                "UPDATE_FIELDS",
                "UPDATE_FIELDS",
                "UPDATE_FIELDS",
            ],
        )

    def test_distinct_cross_meeting_goal_goes_to_review(self) -> None:
        first = candidate(
            "m1-001",
            "ERP财务模块开发",
            "完成财务成本模块需求开发。",
            ["财务成本模块需求开发"],
            "完成财务成本模块需求开发。",
        )
        distinct = candidate(
            "m1-002",
            "ERP系统等保测评采购",
            "组织等保测评服务采购和合同签订。",
            ["等保测评采购", "合同签订"],
            "组织等保测评服务采购和合同签订。",
            department="采购管理部",
        )
        self._import(first, "MEETING_1", "RUN_1")
        report = self._import(distinct, "MEETING_2", "RUN_2")

        self.assertEqual(report["results"][0]["db_action"], "NOOP")
        self.assertEqual(report["m8_review_count"], 1)
        with connect(self.database) as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE project_id = 'P_ERP' AND is_deleted = 0
                """
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_single_concrete_project_candidate_uses_continuity_update(
        self,
    ) -> None:
        first = candidate(
            "m1-001",
            "哈拉沟项目智能分选测试与合同准备",
            "完成智能分选测试，准备项目合同。",
            ["智能分选测试", "准备项目合同"],
            "完成智能分选测试，准备项目合同。",
        )
        later = candidate(
            "m1-002",
            "哈拉沟项目智能化系统调试与大会申报",
            "调试智能化系统并编制科技大会申报书。",
            ["系统调试", "编制科技大会申报书"],
            "调试智能化系统并编制科技大会申报书。",
        )
        self._import(first, "MEETING_1", "RUN_1")
        report = self._import(later, "MEETING_2", "RUN_2")

        self.assertEqual(report["results"][0]["db_action"], "UPDATE_FIELDS")
        self.assertEqual(
            report["match_decisions"][0]["match_mode"],
            "unique_project_continuity",
        )

    def test_multiple_same_meeting_project_candidates_stay_separate(
        self,
    ) -> None:
        first = candidate(
            "m1-001",
            "红沙泉项目现场施工",
            "推进数据中心现场施工。",
            ["数据中心现场施工"],
            "推进数据中心现场施工。",
        )
        second = candidate(
            "m1-002",
            "红沙泉项目平台研发",
            "开发智能综合管控平台。",
            ["智能综合管控平台研发"],
            "开发智能综合管控平台。",
        )
        report = import_candidates(
            {
                "schema_version": "m2.task_gate.v2",
                "source_file": "meeting.pdf",
                "extract_candidates": [first, second],
                "review_candidates": [],
            },
            self.database,
            self.root / "same_meeting.json",
            meeting_id="MEETING_SAME",
            run_id="RUN_SAME",
        )

        self.assertEqual(
            [item["db_action"] for item in report["results"]],
            ["CREATE", "CREATE"],
        )

    def test_broad_project_category_does_not_force_continuity(self) -> None:
        first = candidate(
            "m1-001",
            "安全生产专项分包协议治理",
            "规范分包安全协议。",
            ["规范分包安全协议"],
            "规范分包安全协议。",
            canonical_name="安全生产专项",
        )
        unrelated = candidate(
            "m1-002",
            "五一节前安全检查",
            "开展五一节前安全检查。",
            ["五一节前安全检查"],
            "开展五一节前安全检查。",
            canonical_name="安全生产专项",
        )
        self._import(first, "MEETING_1", "RUN_1")
        report = self._import(unrelated, "MEETING_2", "RUN_2")

        self.assertEqual(report["results"][0]["db_action"], "CREATE")

    def test_ambiguous_match_goes_to_review_without_creating(self) -> None:
        first = candidate(
            "m1-001",
            "ERP项目开发上线推进",
            "推进ERP项目开发和上线。",
            ["项目开发", "系统上线"],
            "推进ERP项目开发和上线。",
        )
        self._import(first, "MEETING_1", "RUN_1")
        with connect(self.database) as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE project_id = 'P_ERP'"
            ).fetchone()
            duplicate = dict(row)
            duplicate["task_id"] = "T_AMBIGUOUS"
            duplicate["source_key"] = "seed:ambiguous"
            columns = list(duplicate)
            connection.execute(
                "INSERT INTO tasks ({}) VALUES ({})".format(
                    ", ".join(columns),
                    ", ".join("?" for _ in columns),
                ),
                [duplicate[name] for name in columns],
            )
            connection.commit()

        similar = candidate(
            "m1-002",
            "ERP项目开发与上线推进",
            "继续推进ERP项目开发与上线。",
            ["继续项目开发", "推进系统上线"],
            "继续推进ERP项目开发与上线。",
        )
        report = self._import(similar, "MEETING_2", "RUN_2")

        self.assertEqual(report["results"][0]["db_action"], "NOOP")
        self.assertEqual(report["m8_review_count"], 1)
        with connect(self.database) as connection:
            active_count = connection.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE project_id = 'P_ERP' AND is_deleted = 0
                """
            ).fetchone()[0]
            review_count = connection.execute(
                """
                SELECT COUNT(*) FROM review_candidates
                WHERE reason_code = 'M8_AMBIGUOUS_PROJECT_TASK'
                """
            ).fetchone()[0]
        self.assertEqual(active_count, 2)
        self.assertEqual(review_count, 1)


class DuplicateHistoryMigrationTest(unittest.TestCase):
    def test_replays_updates_and_soft_deletes_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.db"
            initialize_database(database)
            with connect(database) as connection:
                for index, task_id in enumerate(
                    ("T_CANONICAL", "T_SECOND", "T_THIRD"),
                    start=1,
                ):
                    connection.execute(
                        """
                        INSERT INTO tasks (
                            task_id, title, project_id, description,
                            work_items_json, assignee_raw, deadline_raw,
                            status, source_key, source_meeting_id,
                            source_segment_id, source_subsegment_id,
                            evidence_text, created_at, updated_at
                        ) VALUES (?, ?, 'P_ERP', ?, ?, NULL, NULL,
                                  'in_progress', ?, ?, ?, '001', ?, ?, ?)
                        """,
                        (
                            task_id,
                            "ERP进度{}".format(index),
                            "第{}次进度".format(index),
                            json.dumps(
                                ["进度{}".format(index)],
                                ensure_ascii=False,
                            ),
                            "source:{}".format(index),
                            "MEETING_{}".format(index),
                            "m1-{:03d}".format(index),
                            "第{}次进度".format(index),
                            "2026-04-{:02d}".format(index),
                            "2026-04-{:02d}".format(index),
                        ),
                    )
                connection.commit()

            planned = migrate(
                database,
                "P_ERP",
                "T_CANONICAL",
                ["T_SECOND", "T_THIRD"],
            )
            self.assertEqual(planned["status"], "planned")

            applied = migrate(
                database,
                "P_ERP",
                "T_CANONICAL",
                ["T_SECOND", "T_THIRD"],
                apply=True,
            )
            self.assertEqual(applied["status"], "applied")
            with connect(database) as connection:
                canonical = connection.execute(
                    "SELECT * FROM tasks WHERE task_id = 'T_CANONICAL'"
                ).fetchone()
                active_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM tasks
                    WHERE project_id = 'P_ERP' AND is_deleted = 0
                    """
                ).fetchone()[0]
                actions = connection.execute(
                    """
                    SELECT db_action FROM task_events
                    WHERE run_id = ?
                    ORDER BY created_at, event_id
                    """,
                    ("MIGRATION_DUPLICATE_TASK_HISTORY_V1",),
                ).fetchall()
            self.assertEqual(canonical["title"], "ERP进度3")
            self.assertEqual(canonical["version"], 3)
            self.assertEqual(active_count, 1)
            self.assertEqual(
                sorted(row["db_action"] for row in actions),
                [
                    "SOFT_DELETE",
                    "SOFT_DELETE",
                    "UPDATE_FIELDS",
                    "UPDATE_FIELDS",
                ],
            )

    def test_bulk_plan_skips_broad_and_same_meeting_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demo.db"
            initialize_database(database)
            with connect(database) as connection:
                connection.executescript(PROJECT_SCHEMA)
                for project_id, name in (
                    ("P_GOOD", "哈拉沟项目"),
                    ("P_BROAD", "安全生产专项"),
                    ("P_MULTI", "红沙泉项目"),
                ):
                    connection.execute(
                        """
                        INSERT INTO projects (
                            project_id, canonical_name,
                            created_at, updated_at
                        ) VALUES (?, ?, '2026-04-01', '2026-04-01')
                        """,
                        (project_id, name),
                    )
                for index, (
                    project_id,
                    meeting_id,
                ) in enumerate(
                    (
                        ("P_GOOD", "M1"),
                        ("P_GOOD", "M2"),
                        ("P_BROAD", "M1"),
                        ("P_BROAD", "M2"),
                        ("P_MULTI", "M1"),
                        ("P_MULTI", "M1"),
                    ),
                    start=1,
                ):
                    connection.execute(
                        """
                        INSERT INTO tasks (
                            task_id, title, project_id, description,
                            work_items_json, assignee_raw, deadline_raw,
                            status, source_key, source_meeting_id,
                            source_segment_id, source_subsegment_id,
                            evidence_text, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, '[]', NULL, NULL,
                                  'in_progress', ?, ?, ?, '001',
                                  ?, ?, ?)
                        """,
                        (
                            "T{}".format(index),
                            "任务{}".format(index),
                            project_id,
                            "描述{}".format(index),
                            "source:{}".format(index),
                            meeting_id,
                            "m1-{:03d}".format(index),
                            "证据{}".format(index),
                            "2026-04-{:02d}".format(index),
                            "2026-04-{:02d}".format(index),
                        ),
                    )
                connection.commit()

            plan = plan_migrations(database)

        self.assertEqual(plan["eligible_project_count"], 1)
        self.assertEqual(plan["eligible_group_count"], 1)
        self.assertEqual(plan["eligible"][0]["project_id"], "P_GOOD")
        skipped = {
            item["project_id"]: item["skip_reasons"]
            for item in plan["skipped"]
        }
        self.assertIn("broad_project_category", skipped["P_BROAD"])
        self.assertIn(
            "multiple_tasks_in_same_meeting",
            skipped["P_MULTI"],
        )


if __name__ == "__main__":
    unittest.main()
