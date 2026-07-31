import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from main import load_config, run


class FakeClient:
    def generate(self, system_prompt, user_prompt):
        text = "机电队明天完成检修"
        content = {
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
        }
        return {"content": json.dumps(content, ensure_ascii=False), "model": "fake", "usage": {}}


class MainTests(unittest.TestCase):
    def test_m1_to_m2_vertical_slice(self):
        source = [
            {
                "meeting_title": "早调会",
                "segment_id": "seg-1",
                "speaker_id": "说话人1",
                "speaker_name": None,
                "speaker_role": None,
                "speaker_department": None,
                "timestamp": "00:01",
                "raw_text": "机电队明天完成检修",
                "clean_text": "机电队明天完成检修",
                "quality_flags": [],
                "uncertainties": [],
                "asr_confidence": None,
                "nbest": [],
            }
        ]
        config = load_config(ROOT / "config" / "config.yaml")
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            input_path = temp / "m1.json"
            input_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            results, audit, review = run(
                config,
                client=FakeClient(),
                input_path=input_path,
                output_dir=temp / "output",
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(len(audit), 1)
            self.assertEqual(review, [])
            self.assertTrue((temp / "output" / "m2_classifications.json").exists())

    def test_missing_requested_segment_is_rejected(self):
        source = [
            {
                "meeting_title": "早调会",
                "segment_id": "seg-1",
                "speaker_id": "说话人1",
                "speaker_name": None,
                "speaker_role": None,
                "speaker_department": None,
                "timestamp": "00:01",
                "raw_text": "机电队明天完成检修",
                "clean_text": "机电队明天完成检修",
                "quality_flags": [],
                "uncertainties": [],
                "asr_confidence": None,
                "nbest": [],
            }
        ]
        config = load_config(ROOT / "config" / "config.yaml")
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            input_path = temp / "m1.json"
            input_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                run(
                    config,
                    client=FakeClient(),
                    input_path=input_path,
                    output_dir=temp / "output",
                    segment_ids=["seg-missing"],
                )


if __name__ == "__main__":
    unittest.main()
