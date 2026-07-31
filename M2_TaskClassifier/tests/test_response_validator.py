import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from response_validator import (
    ResponseValidationError,
    enforce_recall_routes,
    load_schema,
    parse_and_validate_response,
)


def valid_result(text="机电队明天完成检修", route="EXTRACT"):
    return {
        "label_code": "T",
        "schema_version": "m2.v1",
        "segment_id": "seg-1",
        "primary_label": "task",
        "secondary_labels": [],
        "event_hint": "CREATE",
        "route": route,
        "signals": {
            "explicit_action": True,
            "explicit_actor": True,
            "explicit_object": True,
            "explicit_deadline": True,
            "negated": False,
            "hypothetical": False,
            "quoted_previous_instruction": False,
            "completed_report": False,
            "safety_related": False,
        },
        "evidence": [{"text": text, "start_char": 0, "end_char": len(text)}],
        "ambiguities": [],
    }


class ResponseValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema(ROOT / "config" / "m2_output.schema.json")

    def test_accepts_exact_evidence(self):
        text = "机电队明天完成检修"
        result = parse_and_validate_response(
            json.dumps(valid_result(text), ensure_ascii=False),
            self.schema,
            "seg-1",
            text,
        )
        self.assertEqual(result["primary_label"], "task")

    def test_rejects_rewritten_evidence(self):
        text = "机电队明天完成检修"
        result = valid_result("机电队完成检修")
        result["evidence"][0]["end_char"] = len("机电队完成检修")
        with self.assertRaises(ResponseValidationError):
            parse_and_validate_response(
                json.dumps(result, ensure_ascii=False), self.schema, "seg-1", text
            )

    def test_unique_evidence_offset_is_recovered(self):
        text = "前文。\n机电队明天完成检修。"
        evidence = "机电队明天完成检修。"
        result = valid_result(evidence)
        result["evidence"][0]["start_char"] = 0
        result["evidence"][0]["end_char"] = len(evidence)
        parsed, normalizations = parse_and_validate_response(
            json.dumps(result, ensure_ascii=False),
            self.schema,
            "seg-1",
            text,
            include_normalizations=True,
        )
        self.assertEqual(parsed["evidence"][0]["text"], evidence)
        self.assertEqual(parsed["evidence"][0]["start_char"], 4)
        self.assertIn("evidence_offset_recovered:0", normalizations)

    def test_unique_whitespace_fold_is_restored_to_source(self):
        text = "一是完成检查。\n二是提交报告。"
        evidence = "一是完成检查。二是提交报告。"
        result = valid_result(evidence)
        parsed, normalizations = parse_and_validate_response(
            json.dumps(result, ensure_ascii=False),
            self.schema,
            "seg-1",
            text,
            include_normalizations=True,
        )
        self.assertEqual(parsed["evidence"][0]["text"], text)
        self.assertEqual(parsed["evidence"][0]["end_char"], len(text))
        self.assertIn("evidence_offset_recovered:0", normalizations)

    def test_candidate_label_cannot_be_discarded(self):
        guarded, overrides = enforce_recall_routes(valid_result(route="DISCARD"), "机电队明天完成检修")
        self.assertEqual(guarded["route"], "EXTRACT")
        self.assertIn("candidate_label_recalled", overrides)

    def test_m1_style_ambiguity_is_losslessly_normalized(self):
        text = "机电队明天完成检修"
        result = valid_result(text)
        result["ambiguities"] = [{"text": "检修", "reason": "对象不明确"}]
        parsed = parse_and_validate_response(
            json.dumps(result, ensure_ascii=False), self.schema, "seg-1", text
        )
        self.assertEqual(
            parsed["ambiguities"],
            [{"field": "content", "description": "检修：对象不明确"}],
        )

    def test_string_ambiguity_is_normalized_without_dropping_text(self):
        text = "机电队明天完成检修"
        result = valid_result(text)
        result["ambiguities"] = ["ASR 内容不清"]
        parsed = parse_and_validate_response(
            json.dumps(result, ensure_ascii=False), self.schema, "seg-1", text
        )
        self.assertEqual(
            parsed["ambiguities"],
            [{"field": "content", "description": "ASR 内容不清"}],
        )

    def test_safety_keyword_forces_review(self):
        result = valid_result("现场存在瓦斯隐患", route="DISCARD")
        result["label_code"] = "N"
        result["primary_label"] = "non_task"
        result["event_hint"] = "NONE"
        result["signals"]["explicit_action"] = False
        result["signals"]["explicit_actor"] = False
        result["signals"]["explicit_object"] = False
        result["signals"]["explicit_deadline"] = False
        guarded, overrides = enforce_recall_routes(result, "现场存在瓦斯隐患")
        self.assertEqual(guarded["route"], "HUMAN_REVIEW")
        self.assertTrue(guarded["signals"]["safety_related"])
        self.assertIn("safety_candidate_review", overrides)


if __name__ == "__main__":
    unittest.main()
