"""Build an unlabeled cross-meeting review pack without inventing gold labels."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.m2_input import load_m2_payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    samples = []
    for path in args.inputs:
        document_id, contexts = load_m2_payload(path, require_pass=True)
        for context in contexts:
            if context.item["item_type"] == "NON_TASK_ITEM":
                continue
            samples.append(
                {
                    "sample_id": f"{document_id}:{context.source_item_id}",
                    "source_document_id": document_id,
                    "source_item_id": context.source_item_id,
                    "source_mode": context.source_mode,
                    "project_entity_id": context.project_entity_id,
                    "current_item": context.item,
                    "historical_candidates": [],
                    "gold": {
                        "decision": None,
                        "target_task_id": None,
                        "annotator_note": "待人工跨会议标注",
                    },
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "schema_version": "m6.lifecycle_gold.v1",
                "label_status": "UNLABELED",
                "samples": samples,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
