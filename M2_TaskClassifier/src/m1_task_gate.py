"""新版 M1 项目级任务的 M2 候选校验入口。"""

import argparse
import json
from pathlib import Path


def text(value):
    return value.strip() if isinstance(value, str) else ""


def text_list(value):
    return [text(item) for item in value if text(item)] if isinstance(value, list) else []


def confidence(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return None


def source_text(task):
    parts = [
        "部门：{}".format(task["department"]),
        "项目：{}".format(task["project"]),
        "任务：{}".format(task["title"]),
        task["description"],
        "证据：{}".format(task["evidence"]),
    ]
    if task["work_items"]:
        parts.append("工作项：" + "；".join(task["work_items"]))
    return "\n".join(part for part in parts if part)


def gate_tasks(m1_result):
    if not isinstance(m1_result, dict) or not isinstance(m1_result.get("tasks"), list):
        raise ValueError("新版 M1 输出必须是包含 tasks 数组的 JSON 对象")
    extract, review = [], []
    for index, raw in enumerate(m1_result["tasks"], start=1):
        if not isinstance(raw, dict):
            review.append({"candidate_id": "m1-{:03d}".format(index), "reason": "task_not_object"})
            continue
        task = {
            "department": text(raw.get("department")),
            "project": text(raw.get("project")),
            "title": text(raw.get("title")),
            "description": text(raw.get("description")),
            "work_items": text_list(raw.get("work_items")),
            "assignee": text(raw.get("assignee")),
            "deadline": text(raw.get("deadline")),
            "priority": text(raw.get("priority")) or "中",
            "status": text(raw.get("status")) or "待执行",
            "evidence": text(raw.get("evidence")),
            "confidence": confidence(raw.get("confidence")),
        }
        candidate = {"candidate_id": "m1-{:03d}".format(index), **task}
        missing = [name for name in ("department", "project", "title", "evidence") if not task[name]]
        if missing:
            review.append({**candidate, "reason": "missing_" + "_".join(missing)})
            continue
        if task["status"] not in {"待执行", "进行中"}:
            review.append({**candidate, "reason": "non_active_initial_status"})
            continue
        if task["confidence"] is not None and task["confidence"] < 0.8:
            review.append({
                **candidate,
                "reason": "low_model_confidence",
                "confidence_source": "model",
            })
            continue
        candidate["route"] = "EXTRACT"
        candidate["source_text"] = source_text(task)
        extract.append(candidate)
    return {
        "schema_version": "m2.task_gate.v2",
        "source_file": text(m1_result.get("source_file")),
        "extract_candidates": extract,
        "review_candidates": review,
    }


def main():
    parser = argparse.ArgumentParser(description="新版 M1 项目任务的 M2 候选校验")
    parser.add_argument("input", type=Path, help="M1 *.tasks.json")
    parser.add_argument("--output", "-o", type=Path, required=True)
    args = parser.parse_args()
    result = gate_tasks(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("M2: 可提交 {} 条，复核 {} 条".format(
        len(result["extract_candidates"]), len(result["review_candidates"])
    ))


if __name__ == "__main__":
    main()
