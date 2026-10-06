"""FastAPI entry point for semantic results and human review."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .database import initialize_database
from .department_workbench import register_department_routes
from .m6.executor import CommandExecutor, CommandExecutionError
from .m6.model_client import (
    OpenAICompatibleReviewClient,
    ReviewClient,
    empty_patch,
)
from .m6.validator import CommandValidator
from .review_plan import suggest_operations, validate_operations
from .task_repository import TaskRepository


TASK_STATUSES = {"open", "in_progress", "blocked", "completed", "cancelled"}
MIN_TRAINING_EVIDENCE_CHARS = 80
TRAINING_EVIDENCE_CONTEXT_NEIGHBORS = 2


def _validate_task_payload(payload: dict, *, creating: bool = False) -> dict:
    allowed = {
        "title", "description", "work_items", "assignee_raw", "deadline_raw",
        "department", "project", "priority", "status", "evidence_text", "source_meeting_id",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持的字段：{sorted(unknown)}")
    result = dict(payload)
    if creating:
        for name in ("title", "evidence_text"):
            if not str(result.get(name) or "").strip():
                raise HTTPException(status_code=400, detail=f"{name} 为必填项")
    if "title" in result and not str(result["title"] or "").strip():
        raise HTTPException(status_code=400, detail="title 不能为空")
    if "status" in result and result["status"] not in TASK_STATUSES:
        raise HTTPException(status_code=400, detail="任务状态无效")
    if "work_items" in result:
        if not isinstance(result["work_items"], list) or any(
            not isinstance(item, str) or not item.strip() for item in result["work_items"]
        ):
            raise HTTPException(status_code=400, detail="work_items 必须是非空字符串列表")
    return result


def _training_sample(task: dict) -> dict:
    return {
        "schema_version": "meeting_task_training_sample_v1",
        "sample_id": task["task_id"],
        "original_evidence": task["evidence_text"],
        "label": {
            name: task.get(name)
            for name in (
                "title", "description", "work_items", "department", "project",
                "priority", "assignee_raw", "deadline_raw", "status",
            )
        },
        "provenance": {
            name: task.get(name)
            for name in (
                "source_meeting_id", "source_segment_id", "source_subsegment_id",
                "source_key", "created_at", "updated_at",
            )
        },
    }


def _source_sort_key(task: dict) -> tuple:
    """Order source identifiers naturally (for example m1-2 before m1-10)."""
    identifier = "/".join(
        str(task.get(name) or "")
        for name in ("source_segment_id", "source_subsegment_id", "task_id")
    )
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", identifier)
        if part
    )


def _training_evidence_texts(
    tasks: list[dict],
    repository: TaskRepository,
    neighbor_count: int = TRAINING_EVIDENCE_CONTEXT_NEIGHBORS,
) -> dict[str, str]:
    """Embed bounded, same-meeting context directly in each training input."""
    by_meeting: dict[str, list[dict]] = {}
    for task in tasks:
        meeting_id = str(task.get("source_meeting_id") or "")
        if meeting_id:
            by_meeting.setdefault(meeting_id, []).append(task)

    evidence_texts = {
        task["task_id"]: str(task.get("evidence_text") or "").strip()
        for task in tasks
    }
    for meeting_id, meeting_tasks in by_meeting.items():
        meeting = repository.get_meeting(meeting_id)
        ordered = sorted(meeting_tasks, key=_source_sort_key)
        contextual_tasks = [
            task for task in ordered
            if meeting is not None
            and str(task.get("source_segment_id") or "").lower() != "manual"
        ]
        contextual_indices = {
            task["task_id"]: index for index, task in enumerate(contextual_tasks)
        }
        for task in ordered:
            index = contextual_indices.get(task["task_id"])
            if index is None:
                continue
            start = max(0, index - neighbor_count)
            end = min(len(contextual_tasks), index + neighbor_count + 1)
            previous = contextual_tasks[start:index]
            following = contextual_tasks[index + 1:end]
            if not previous and not following:
                continue

            meeting_lines = [
                f"会议日期：{meeting.get('meeting_date') or ''}",
                f"会议类型：{meeting.get('meeting_type') or ''}",
                f"会议标题：{meeting.get('meeting_title') or ''}",
            ]
            if meeting.get("leader_requirements"):
                meeting_lines.append(f"领导要求：{meeting['leader_requirements']}")

            def render(items: list[dict]) -> list[str]:
                return [
                    (
                        f"[{item.get('source_segment_id')}/"
                        f"{item.get('source_subsegment_id')}] "
                        f"{str(item.get('evidence_text') or '').strip()}"
                    )
                    for item in items
                ]

            parts = ["【会议信息】", *meeting_lines]
            if previous:
                parts.extend(["【前序会议原文】", *render(previous)])
            parts.extend([
                "【目标原文证据】",
                evidence_texts[task["task_id"]],
            ])
            if following:
                parts.extend(["【后续会议原文】", *render(following)])
            evidence_texts[task["task_id"]] = "\n".join(parts)
    return evidence_texts


def create_app(
    settings: Settings | None = None,
    review_client: ReviewClient | None = None,
) -> FastAPI:
    resolved = settings or Settings.load()
    resolved.runs_dir.mkdir(parents=True, exist_ok=True)
    resolved.static_dir.mkdir(parents=True, exist_ok=True)
    initialize_database(
        resolved.database_path,
        seed_demo_data=resolved.seed_demo_data,
    )
    repository = TaskRepository(resolved.database_path)
    client = review_client or OpenAICompatibleReviewClient(
        base_url=resolved.llm_base_url,
        model=resolved.llm_model,
        api_key=resolved.llm_api_key,
        timeout_seconds=resolved.llm_timeout_seconds,
    )
    validator = CommandValidator()
    executor = CommandExecutor(resolved.database_path)

    app = FastAPI(
        title="会议语义结果查询与人工复核",
        version="0.1.0",
    )
    app.state.settings = resolved
    app.state.repository = repository
    register_department_routes(app, repository, resolved.static_dir)
    app.mount(
        "/static",
        StaticFiles(directory=resolved.static_dir),
        name="static",
    )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "database": "ready"}

    @app.post("/api/demo/runs")
    def create_run() -> dict:
        raise HTTPException(
            status_code=503,
            detail=(
                "网页自动导入尚未接入当前语义链路。"
                "请使用 M1→M3→M6 批处理生成结果，再导出到展示库。"
            ),
        )

    @app.get("/api/demo/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = repository.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        result = {
            key: value for key, value in run.items() if key != "m2_count"
        }
        if isinstance(result.get("summary"), dict):
            result["summary"] = {
                key: value for key, value in result["summary"].items()
                if not key.startswith("m2_")
            }
        return result

    @app.get("/api/demo/runs/{run_id}/summary")
    def get_run_summary(run_id: str) -> dict:
        run = repository.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        return {
            "run_id": run_id,
            "status": run["status"],
            "m1_count": run["m1_count"],
            "command_count": run["command_count"],
            "applied_count": run["applied_count"],
            "noop_count": run["noop_count"],
            "failed_count": run["failed_count"],
            "summary": {
                key: value for key, value in (run.get("summary") or {}).items()
                if not key.startswith("m2_")
            },
        }

    @app.get("/api/demo/runs/{run_id}/events")
    def get_run_events(run_id: str) -> list[dict]:
        if repository.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="运行不存在")
        return repository.list_events(run_id=run_id)

    @app.get("/api/events")
    def get_events() -> list[dict]:
        """Return the persisted operation history across all runs and manual edits."""
        return repository.list_events()

    @app.get("/api/tasks")
    def get_tasks(
        status: str | None = Query(default=None),
        include_deleted: bool = Query(default=False),
        meeting_id: str | None = Query(default=None),
    ) -> list[dict]:
        allowed = {None, "open", "in_progress", "blocked", "completed", "cancelled"}
        if status not in allowed:
            raise HTTPException(status_code=400, detail="任务状态无效")
        return repository.list_tasks(
            status=status,
            include_deleted=include_deleted,
            meeting_id=meeting_id,
        )

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        task = repository.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {
            "task": task,
            "events": repository.list_events(task_id=task_id),
            "meeting": repository.get_meeting(task["source_meeting_id"]),
        }

    @app.get("/api/tasks/{task_id}/history")
    def get_task_history(task_id: str) -> list[dict]:
        try:
            return repository.list_task_history(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @app.post("/api/tasks", status_code=201)
    def create_manual_task(payload: dict) -> dict:
        fields = _validate_task_payload(payload, creating=True)
        meeting_id = str(fields.get("source_meeting_id") or "").strip()
        if meeting_id and repository.get_meeting(meeting_id) is None:
            raise HTTPException(status_code=400, detail="归属会议不存在")
        try:
            return repository.create_manual_task(fields)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: str, payload: dict) -> dict:
        fields = _validate_task_payload(payload)
        fields.pop("evidence_text", None)  # 原文证据仅能随原始来源写入，不可人工覆盖。
        try:
            return repository.update_task_by_human(task_id, fields)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str) -> dict:
        try:
            return repository.soft_delete_task_by_human(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/training-samples/export")
    def export_training_samples() -> Response:
        tasks = [
            task for task in repository.list_tasks()
            if len(str(task.get("evidence_text") or "").strip()) >= MIN_TRAINING_EVIDENCE_CHARS
        ]
        if not tasks:
            raise HTTPException(
                status_code=409,
                detail=f"没有原文证据不少于 {MIN_TRAINING_EVIDENCE_CHARS} 字的可导出样本",
            )
        archive = io.BytesIO()
        manifest: list[dict] = []
        evidence_texts = _training_evidence_texts(tasks, repository)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for task in tasks:
                sample = _training_sample(task)
                folder = f"samples/{task['task_id']}"
                evidence_name = f"{folder}/evidence.txt"
                json_name = f"{folder}/label.json"
                training_evidence = evidence_texts[task["task_id"]]
                bundle.writestr(evidence_name, training_evidence)
                bundle.writestr(json_name, json.dumps(sample, ensure_ascii=False, indent=2))
                manifest.append({
                    "sample_id": task["task_id"], "evidence_file": evidence_name,
                    "label_file": json_name, "evidence_chars": len(training_evidence),
                })
            bundle.writestr("manifest.json", json.dumps({
                "schema_version": "meeting_task_training_export_v1",
                "minimum_evidence_chars": MIN_TRAINING_EVIDENCE_CHARS,
                "sample_count": len(manifest), "samples": manifest,
            }, ensure_ascii=False, indent=2))
        return Response(
            content=archive.getvalue(), media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=meeting-task-training-samples.zip"},
        )

    @app.get("/api/meetings")
    def get_meetings() -> list[dict]:
        return repository.list_meetings()

    @app.get("/api/review-candidates")
    def get_review_candidates(status: str = Query(default="pending")) -> list[dict]:
        if status not in {"pending", "approved", "rejected"}:
            raise HTTPException(status_code=400, detail="invalid review status")
        return repository.list_review_candidates(status)

    @app.patch("/api/review-candidates/{candidate_id}")
    def update_review_candidate(candidate_id: str, payload: dict) -> dict:
        if 'operations' in payload:
            review = repository.get_review_candidate(candidate_id)
            if review is None:
                raise HTTPException(status_code=404, detail='复核记录不存在')
            try:
                validate_operations(payload['operations'], review['candidate'])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if payload.get("initial_status") not in {None, "open", "in_progress"}:
            raise HTTPException(status_code=400, detail="复核任务初始状态只能是 open/in_progress")
        if "work_items" in payload and (
            not isinstance(payload["work_items"], list)
            or any(not isinstance(item, str) or not item.strip() for item in payload["work_items"])
        ):
            raise HTTPException(status_code=400, detail="work_items 必须是非空字符串列表")
        try:
            return repository.update_review_candidate(candidate_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/review-candidates/{candidate_id}/plan')
    def get_review_plan(candidate_id: str) -> dict:
        review = repository.get_review_candidate(candidate_id)
        if review is None:
            raise HTTPException(status_code=404, detail='复核记录不存在')
        if review['status'] != 'pending':
            raise HTTPException(status_code=409, detail='复核记录已处理')
        tasks = {task['task_id']: task for task in repository.list_tasks(include_deleted=True)}
        if 'operations' in review['candidate']:
            return {'operations': review['candidate']['operations'], 'saved': True,
                    'tasks': [tasks[t['task_id']] for t in review['candidate'].get('candidate_tasks', []) if t.get('task_id') in tasks]}
        try:
            operations = suggest_operations(review, tasks, client)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail='建议生成服务暂不可用，可手工添加表单或重试') from exc
        return {'operations': operations, 'saved': False,
                'tasks': [tasks[t['task_id']] for t in review['candidate'].get('candidate_tasks', []) if t.get('task_id') in tasks]}

    @app.post("/api/review-candidates/{candidate_id}/approve")
    def approve_review_candidate(candidate_id: str, payload: dict) -> dict:
        if 'operations' in payload:
            try:
                note = payload.get('reviewer_note') or ''
                if not isinstance(note, str):
                    raise ValueError('复核备注必须是文本')
                return executor.execute_review(candidate_id, payload['operations'], note)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except CommandExecutionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        review = repository.get_review_candidate(candidate_id)
        if review is None:
            raise HTTPException(status_code=404, detail="review candidate not found")
        if review["status"] != "pending":
            raise HTTPException(status_code=409, detail="review candidate already resolved")
        candidate = review["candidate"]
        title = str(payload.get("title") or candidate.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title is required")
        evidence = str(candidate.get("evidence") or candidate.get("source_text") or "").strip()
        if not evidence:
            raise HTTPException(status_code=400, detail="candidate has no source evidence")
        work_items = payload.get("work_items", candidate.get("work_items") or [])
        if not isinstance(work_items, list):
            raise HTTPException(status_code=400, detail="work_items must be a list")
        status = str(payload.get("status") or candidate.get("initial_status") or "open")
        if status not in {"open", "in_progress"}:
            raise HTTPException(status_code=400, detail="invalid initial status")
        description = str(payload.get("description") or candidate.get("description") or title)
        source = {
            "segment_id": f"review_{candidate_id}",
            "subsegment_id": "001",
            "text": evidence,
        }
        command = {
            "command_index": 1,
            "db_action": "CREATE",
            "target_task_id": None,
            "expected_version": None,
            "task_patch": empty_patch(
                title=title,
                description=description,
                work_items=work_items,
                assignee_raw=payload.get("assignee_raw") or candidate.get("assignee") or None,
                deadline_raw=payload.get("deadline_raw") or candidate.get("deadline") or None,
                status=status,
            ),
            "evidence": {
                "segment_id": source["segment_id"],
                "subsegment_id": "001",
                "text": evidence,
            },
            "reason_code": "HUMAN_REVIEW_APPROVED",
            "ambiguities": [],
        }
        batch = {
            "schema_version": "m6_db_command_v1",
            "source_key": f"{review['meeting_id']}:{source['segment_id']}:001",
            "commands": [command],
        }
        try:
            validator.validate_batch(
                batch, source, review["meeting_id"], {"candidates": []}
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = executor.execute_command(
            batch["source_key"], command, review["run_id"], review["meeting_id"]
        )
        if result["execution_status"] not in {"applied", "noop", "duplicate"}:
            raise HTTPException(status_code=409, detail=result.get("failure_reason", "write failed"))
        metadata = {
            name: payload.get(name, candidate.get(name))
            for name in ("department", "project", "priority")
            if payload.get(name, candidate.get(name)) is not None
        }
        if metadata and result.get("task_id"):
            repository.update_task_by_human(result["task_id"], metadata)
        repository.resolve_review_candidate(
            candidate_id,
            "approved",
            task_id=result.get("task_id"),
            reviewer_note=str(payload.get("reviewer_note") or "") or None,
        )
        return result

    @app.post("/api/review-candidates/{candidate_id}/reject")
    def reject_review_candidate(candidate_id: str, payload: dict | None = None) -> dict:
        review = repository.get_review_candidate(candidate_id)
        if review is None:
            raise HTTPException(status_code=404, detail="review candidate not found")
        try:
            repository.resolve_review_candidate(
                candidate_id,
                "rejected",
                reviewer_note=str((payload or {}).get("reviewer_note") or "") or None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="review candidate already resolved") from exc
        return {"candidate_id": candidate_id, "status": "rejected"}

    @app.get("/")
    def index() -> FileResponse:
        index_path = resolved.static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=503, detail="前端资源尚未部署")
        return FileResponse(index_path, headers={"Cache-Control": "no-store"})

    return app
