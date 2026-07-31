"""M1 clean_segments.json 到提示词版 M2 分类结果的命令行入口。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import yaml

from classifier import classify_records
from io_utils import atomic_write_json, load_m1_records
from model_client import ModelClient, OpenAICompatibleClient
from prompt_builder import load_system_prompt
from response_validator import load_schema, validate_batch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("M2 配置根节点必须是对象")
    for section in ("input", "output", "resources", "llm", "processing"):
        if section not in data:
            raise ValueError(f"M2 配置缺少节：{section}")
    return data


def output_paths(config: dict, output_dir: Optional[Path]) -> tuple[Path, Path, Path]:
    configured = config["output"]
    paths = (
        resolve_project_path(configured["classifications"]),
        resolve_project_path(configured["audit"]),
        resolve_project_path(configured["review_queue"]),
    )
    if output_dir is None:
        return paths
    return tuple(output_dir / path.name for path in paths)


def run(
    config: dict,
    client: Optional[ModelClient] = None,
    input_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    limit_segments: Optional[int] = None,
    segment_ids: Optional[list[str]] = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    source = input_path or resolve_project_path(config["input"]["m1_json"])
    records = load_m1_records(source)
    if segment_ids:
        requested = set(segment_ids)
        available = {record["segment_id"] for record in records}
        missing = sorted(requested - available)
        if missing:
            raise ValueError(f"M1 输入中不存在 segment_id：{', '.join(missing)}")
        records = [record for record in records if record["segment_id"] in requested]
    if limit_segments is not None:
        if limit_segments <= 0:
            raise ValueError("limit_segments 必须大于 0")
        records = records[:limit_segments]

    schema = load_schema(resolve_project_path(config["resources"]["output_schema"]))
    system_prompt = load_system_prompt(
        resolve_project_path(config["resources"]["system_prompt"])
    )
    model_client = client or OpenAICompatibleClient(config["llm"])
    processing = config["processing"]
    results, audit, review_queue = classify_records(
        records=records,
        system_prompt=system_prompt,
        schema=schema,
        client=model_client,
        context_chars=int(processing.get("context_chars", 300)),
        repair_attempts=int(processing.get("repair_attempts", 1)),
    )
    validate_batch(results, schema)

    classification_path, audit_path, review_path = output_paths(config, output_dir)
    atomic_write_json(results, classification_path)
    atomic_write_json(audit, audit_path)
    atomic_write_json(review_queue, review_path)
    print(f"M2 处理完成：输入 {len(records)} 条，成功 {len(results)} 条，复核 {len(review_queue)} 条")
    print(f"分类结果：{classification_path}")
    print(f"模型审计：{audit_path}")
    print(f"复核队列：{review_path}")
    return results, audit, review_queue


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="提示词版 M2 任务候选分类器")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--limit-segments", type=int)
    parser.add_argument(
        "--segment-id",
        action="append",
        dest="segment_ids",
        help="只回归指定 segment_id，可重复传入",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    _, _, review_queue = run(
        config=config,
        input_path=args.input,
        output_dir=args.output_dir,
        limit_segments=args.limit_segments,
        segment_ids=args.segment_ids,
    )
    if review_queue:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
