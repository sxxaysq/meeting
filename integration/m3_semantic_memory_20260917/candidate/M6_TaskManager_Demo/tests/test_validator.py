from __future__ import annotations

import unittest

from app.m6.model_client import empty_patch
from app.m6.validator import CommandValidationError, CommandValidator

from tests.helpers import command_batch


class CommandValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = CommandValidator()
        self.meeting_id = "DEMO_TEST"
        self.source = {
            "segment_id": "seg_001",
            "subsegment_id": "001",
            "actor_hint": "机电队",
            "text": "机电队月底前提交应急方案。",
            "task_category": "task",
        }

    def test_valid_create(self) -> None:
        batch = command_batch(
            self.source,
            self.meeting_id,
            "CREATE",
            patch=empty_patch(
                title="提交应急方案",
                description="机电队提交应急方案",
                assignee_raw="机电队",
                deadline_raw="月底前",
                status="open",
            ),
        )
        self.assertIs(
            self.validator.validate_batch(
                batch, self.source, self.meeting_id, {"candidates": []}
            ),
            batch,
        )

    def test_update_requires_unique_target(self) -> None:
        batch = command_batch(
            self.source,
            self.meeting_id,
            "UPDATE_STATUS",
            patch=empty_patch(status="completed"),
            target_task_id="T000001",
            expected_version=1,
        )
        with self.assertRaisesRegex(
            CommandValidationError, "没有唯一目标"
        ):
            self.validator.validate_batch(
                batch, self.source, self.meeting_id, {"candidates": []}
            )

    def test_forged_evidence_is_rejected(self) -> None:
        batch = command_batch(
            self.source,
            self.meeting_id,
            "CREATE",
            patch=empty_patch(title="提交应急方案", status="open"),
        )
        batch["commands"][0]["evidence"]["text"] = "会议没有说过的内容"
        with self.assertRaisesRegex(CommandValidationError, "逐字片段"):
            self.validator.validate_batch(
                batch, self.source, self.meeting_id, {"candidates": []}
            )


if __name__ == "__main__":
    unittest.main()

