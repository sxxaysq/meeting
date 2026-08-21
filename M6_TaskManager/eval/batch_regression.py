"""Repeat live lifecycle evaluation and preserve every raw result."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from evaluate_lifecycle import evaluate, load_samples
from src.lifecycle_judge import LifecycleJudge
from src.llm_client import OpenAICompatibleLifecycleClient


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gold", type=Path, default=ROOT / "gold" / "lifecycle_gold_v1.json"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--base-url",
        default=os.getenv("LLM_BASE_URL", "http://192.168.30.215:8000/v1"),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LLM_MODEL", "Qwen/Qwen3.6-35B-A3B"),
    )
    parser.add_argument("--api-key", default=os.getenv("LLM_API_KEY", "EMPTY"))
    args = parser.parse_args()
    samples = load_samples(args.gold)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for index in range(args.repeat):
        judge = LifecycleJudge(
            OpenAICompatibleLifecycleClient(
                base_url=args.base_url,
                model=args.model,
                api_key=args.api_key,
                timeout_seconds=180,
            )
        )
        result = evaluate(samples, mode="llm", judge=judge)
        (args.output_dir / f"run{index}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summaries.append(result["metrics"])
    aggregate = {
        "repeat": args.repeat,
        "runs": summaries,
        "decision_accuracy_values": [
            summary["decision_accuracy"] for summary in summaries
        ],
        "false_update_rate_values": [
            summary["false_update_rate"] for summary in summaries
        ],
        "wrong_target_rate_values": [
            summary["wrong_target_rate"] for summary in summaries
        ],
    }
    (args.output_dir / "repeat_summary.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
