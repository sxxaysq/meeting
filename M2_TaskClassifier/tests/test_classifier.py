import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from classifier import build_context_records, classify_one
from response_validator import load_schema


class FakeClient:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0

    def generate(self, system_prompt, user_prompt):
        content = self.contents[self.calls]
        self.calls += 1
        return {"content": content, "model": "fake", "usage": {}}


def record(segment_id="seg-1", text="机电队明天完成检修", meeting="早调会"):
    return {
        "meeting_title": meeting,
        "segment_id": segment_id,
        "speaker_id": "说话人1",
        "speaker_name": None,
        "speaker_role": None,
        "speaker_department": None,
        "timestamp": "00:01",
        "raw_text": text,
        "clean_text": text,
        "quality_flags": [],
        "uncertainties": [],
        "asr_confidence": None,
        "nbest": [],
    }


def response(text="机电队明天完成检修"):
    return json.dumps(
        {
            "label_code": "T",
            "schema_version": "m2.v1",
            "segment_id": "seg-1",
            "primary_label": "task",
            "secondary_labels": [],
            "event_hint": "CREATE",
            "route": "EXTRACT",
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
        },
        ensure_ascii=False,
    )


class ClassifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema(ROOT / "config" / "m2_output.schema.json")

    def test_invalid_response_is_repaired_once(self):
        client = FakeClient(["not json", response()])
        result, audit = classify_one(
            record(), "", "", "system", self.schema, client, repair_attempts=1
        )
        self.assertEqual(result["label_code"], "T")
        self.assertEqual(client.calls, 2)
        self.assertEqual(audit["attempts"][0]["validation"], "failed")
        self.assertEqual(audit["attempts"][1]["validation"], "passed")

    def test_context_does_not_cross_meetings(self):
        records = [
            record("a", "第一场末尾", "会议A"),
            record("b", "第二场开头", "会议B"),
        ]
        windows = build_context_records(records, 30)
        self.assertEqual(windows[0]["context_after"], "")
        self.assertEqual(windows[1]["context_before"], "")


if __name__ == "__main__":
    unittest.main()
