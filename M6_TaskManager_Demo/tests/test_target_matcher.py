from __future__ import annotations

import unittest

from app.m6.target_matcher import TargetMatcher


TASKS = [
    {
        "task_id": "T000001",
        "title": "提交3125设备应急方案",
        "description": "机电队提交3125设备应急方案。",
        "assignee_raw": "机电队",
        "deadline_raw": "本周五前",
        "status": "open",
        "version": 1,
    },
    {
        "task_id": "T000002",
        "title": "完成主井设备检查",
        "description": "运输队完成主井设备检查。",
        "assignee_raw": "运输队",
        "deadline_raw": None,
        "status": "in_progress",
        "version": 1,
    },
]


class TargetMatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = TargetMatcher()

    def test_explicit_task_id_wins(self) -> None:
        result = self.matcher.match("T000002 这项任务取消", TASKS)
        self.assertEqual(result["unique_target_task_id"], "T000002")
        self.assertEqual(result["match_mode"], "explicit_task_id")

    def test_exact_title_is_unique(self) -> None:
        result = self.matcher.match(
            "原定周五提交3125设备应急方案延期到月底", TASKS
        )
        self.assertEqual(result["unique_target_task_id"], "T000001")
        self.assertEqual(result["match_mode"], "exact_title")

    def test_ambiguous_text_does_not_guess(self) -> None:
        result = self.matcher.match("这项工作继续推进", TASKS)
        self.assertIsNone(result["unique_target_task_id"])


if __name__ == "__main__":
    unittest.main()

