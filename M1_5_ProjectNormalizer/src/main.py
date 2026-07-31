"""M1.5 CLI：为 M2 候选补充已审计项目归属。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from project_catalog import ProjectCatalog
from resolver import OpenAICompatibleProjectModel, ProjectResolver


def project_m2_payload(payload: dict, resolver: ProjectResolver) -> dict:
    """Attach a project context to M2 candidates and replace only its project label.

    M2 remains the source of the task title, description and work items.  M1.5
    only establishes the parent-project identity used by M6's project field.
    """
    if payload.get("schema_version") != "m2.task_gate.v2":
        raise ValueError("M1.5 只接受 m2.task_gate.v2 输出")
    candidates = payload.get("extract_candidates")
    if not isinstance(candidates, list):
        raise ValueError("M1.5 extract_candidates 必须是数组")
    projected = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("M1.5 候选任务必须是对象")
        item = dict(candidate)
        source_text = str(item.get("source_text") or item.get("title") or "")
        context = resolver.resolve_record(
            {
                "task_text": source_text,
                "project_anchor": item.get("project"),
            }
        )
        item["project_context"] = context
        canonical_name = context.get("canonical_name")
        if isinstance(canonical_name, str) and canonical_name.strip():
            item["project"] = canonical_name
        projected.append(item)
    result = dict(payload)
    result["extract_candidates"] = projected
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="M1.5 项目归并")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--base-url", default=os.getenv("M6_LLM_BASE_URL", "http://192.168.30.215:8000/v1"))
    parser.add_argument("--model", default=os.getenv("M6_LLM_MODEL", "Qwen/Qwen3.6-35B-A3B"))
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("M1.5 输入必须是 M2 JSON 对象")
    resolver = ProjectResolver(
        ProjectCatalog(args.database),
        OpenAICompatibleProjectModel(args.base_url, args.model),
    )
    output = project_m2_payload(payload, resolver)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / "m1_5_projected_tasks.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(
        "M1.5 完成：{} 条，项目表：{}".format(
            len(output["extract_candidates"]), args.database
        )
    )


if __name__ == "__main__":
    main()
