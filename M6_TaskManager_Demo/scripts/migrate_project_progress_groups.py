"""Plan and apply conservative project-progress history migrations."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import connect, initialize_database
from app.m6.project_task_matcher import ProjectTaskMatcher
from scripts.migrate_duplicate_task_history import migrate


PROJECT_CONTINUITY_THRESHOLD = 0.25


def _is_concrete_project(name: str) -> bool:
    return (
        ("项目" in name or "系统" in name)
        and not name.endswith("专项")
    )


def _department(description: str | None) -> str:
    for line in str(description or "").splitlines():
        if line.startswith("部门："):
            return line.split("：", 1)[1].strip()
    return ""


def _candidate_from_task(task: dict) -> dict:
    return {
        "title": task["title"],
        "description": task.get("description"),
        "work_items": task.get("work_items", []),
        "department": _department(task.get("description")),
        "assignee": task.get("assignee_raw") or "",
    }


def _continuity_clusters(tasks: list[dict]) -> list[list[dict]]:
    clusters: list[list[dict]] = []
    for task in tasks:
        if not clusters:
            clusters.append([task])
            continue
        previous = clusters[-1][-1]
        score, _ = ProjectTaskMatcher._score(
            _candidate_from_task(task),
            previous,
        )
        task["previous_similarity"] = round(score, 4)
        if score >= PROJECT_CONTINUITY_THRESHOLD:
            clusters[-1].append(task)
        else:
            clusters.append([task])
    return clusters


def plan_migrations(
    database: Path,
    project_ids: set[str] | None = None,
) -> dict:
    initialize_database(database)
    with connect(database) as connection:
        rows = connection.execute(
            """
            SELECT t.project_id, p.canonical_name, t.task_id, t.title,
                   t.description, t.work_items_json, t.assignee_raw,
                   t.source_meeting_id, t.created_at
            FROM tasks t
            JOIN projects p ON p.project_id = t.project_id
            WHERE t.is_deleted = 0
              AND t.status NOT IN ('completed', 'cancelled')
            ORDER BY t.project_id, t.created_at, t.task_id
            """
        ).fetchall()

    grouped: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for row in rows:
        item = dict(row)
        try:
            item["work_items"] = json.loads(
                item.get("work_items_json") or "[]"
            )
        except (TypeError, json.JSONDecodeError):
            item["work_items"] = []
        project_id = item["project_id"]
        if project_ids is not None and project_id not in project_ids:
            continue
        grouped.setdefault(project_id, []).append(item)
        names[project_id] = item["canonical_name"]

    eligible = []
    skipped = []
    for project_id, tasks in grouped.items():
        if len(tasks) < 2:
            continue
        name = names[project_id]
        meeting_counts = Counter(
            task["source_meeting_id"] for task in tasks
        )
        reasons = []
        if not _is_concrete_project(name):
            reasons.append("broad_project_category")
        if max(meeting_counts.values()) > 1:
            reasons.append("multiple_tasks_in_same_meeting")

        base_item = {
            "project_id": project_id,
            "canonical_name": name,
            "task_count": len(tasks),
            "tasks": [
                {
                    "task_id": task["task_id"],
                    "title": task["title"],
                    "source_meeting_id": task["source_meeting_id"],
                    "created_at": task["created_at"],
                    "previous_similarity": task.get(
                        "previous_similarity"
                    ),
                }
                for task in tasks
            ],
        }
        if reasons:
            base_item["skip_reasons"] = reasons
            skipped.append(base_item)
            continue

        for cluster in _continuity_clusters(tasks):
            item = {
                "project_id": project_id,
                "canonical_name": name,
                "task_count": len(cluster),
                "canonical_task_id": cluster[0]["task_id"],
                "duplicate_task_ids": [
                    task["task_id"] for task in cluster[1:]
                ],
                "tasks": [
                    {
                        "task_id": task["task_id"],
                        "title": task["title"],
                        "source_meeting_id": task[
                            "source_meeting_id"
                        ],
                        "created_at": task["created_at"],
                        "previous_similarity": task.get(
                            "previous_similarity"
                        ),
                    }
                    for task in cluster
                ],
            }
            if len(cluster) >= 2:
                eligible.append(item)
            else:
                item["skip_reasons"] = [
                    "insufficient_project_continuity"
                ]
                skipped.append(item)

    return {
        "status": "planned",
        "eligible_group_count": len(eligible),
        "eligible_project_count": len({
            item["project_id"] for item in eligible
        }),
        "eligible_task_count": sum(
            item["task_count"] for item in eligible
        ),
        "duplicate_task_count": sum(
            len(item["duplicate_task_ids"]) for item in eligible
        ),
        "skipped_project_count": len(skipped),
        "eligible": eligible,
        "skipped": skipped,
    }


def apply_migrations(database: Path, plan: dict) -> dict:
    results = []
    for item in plan["eligible"]:
        result = migrate(
            database,
            item["project_id"],
            item["canonical_task_id"],
            item["duplicate_task_ids"],
            apply=True,
        )
        results.append({
            "project_id": item["project_id"],
            "canonical_name": item["canonical_name"],
            "canonical_task_id": item["canonical_task_id"],
            "soft_deleted_count": len(item["duplicate_task_ids"]),
            "status": result["status"],
        })
    return {
        **plan,
        "status": "applied",
        "applied_project_count": len(results),
        "soft_deleted_count": sum(
            item["soft_deleted_count"] for item in results
        ),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "将具体项目的跨会议单任务记录重放为 UPDATE；"
            "宽泛专项和同场多任务自动跳过"
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--project-id",
        action="append",
        dest="project_ids",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    selected = set(args.project_ids) if args.project_ids else None
    plan = plan_migrations(args.database, selected)
    result = apply_migrations(args.database, plan) if args.apply else plan
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
