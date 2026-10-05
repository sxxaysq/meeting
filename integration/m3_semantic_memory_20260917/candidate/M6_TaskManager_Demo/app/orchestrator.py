"""M1→M2→M1.5→M6→SQLite 的单运行编排。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import traceback
from pathlib import Path

from .config import Settings
from .database import utc_now
from .m6.service import TaskAutomationService, load_m2_records
from .task_repository import TaskRepository


class PipelineError(RuntimeError):
    """外部阶段退出异常或产物缺失。"""


class PipelineOrchestrator:
    def __init__(
        self,
        settings: Settings,
        repository: TaskRepository,
        automation: TaskAutomationService,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.automation = automation
        self._run_lock = threading.Lock()

    def process_run(
        self,
        run_id: str,
        meeting_id: str,
        meeting_date: str,
        meeting_type: str,
        input_path: Path | str,
    ) -> None:
        input_file = Path(input_path)
        run_dir = self.settings.runs_dir / run_id
        m1_dir = run_dir / "m1"
        m2_dir = run_dir / "m2"
        m1_5_dir = run_dir / "m1_5"
        m6_dir = run_dir / "m6"
        try:
            with self._run_lock:
                self._validate_local_pipeline()
                self.repository.update_run(run_id, status="running_m1")
                self._run_stage(
                    [
                        str(self.settings.pipeline_python),
                        "src/main.py",
                        str(input_file),
                        "--output",
                        str(m1_dir / "tasks.json"),
                    ],
                    cwd=self.settings.m1_project,
                    log_path=run_dir / "m1.log",
                    env=self._pipeline_environment(),
                )
                m1_output = m1_dir / "tasks.json"
                m1_payload = self._load_object(m1_output, "M1")
                m1_records = self._required_list(m1_payload, "tasks", "M1")
                self.repository.update_run(
                    run_id,
                    status="running_m2",
                    m1_count=len(m1_records),
                )

                self._run_stage(
                    [
                        str(self.settings.pipeline_python),
                        "src/m1_task_gate.py",
                        str(m1_output),
                        "--output",
                        str(m2_dir / "task_gate.json"),
                    ],
                    cwd=self.settings.m2_project,
                    log_path=run_dir / "m2.log",
                )
                m2_output = m2_dir / "task_gate.json"
                m2_payload = self._load_object(m2_output, "M2")
                m2_records = self._required_list(
                    m2_payload, "extract_candidates", "M2"
                )
                review_candidates = m2_payload.get("review_candidates", [])
                if not isinstance(review_candidates, list):
                    raise PipelineError("M2 review_candidates must be a list")
                self.repository.enqueue_review_candidates(
                    run_id, meeting_id, review_candidates
                )
                self.repository.update_run(
                    run_id,
                    status="running_m1_5",
                    m2_count=len(m2_records),
                )

                self._run_stage(
                    [
                        str(self.settings.pipeline_python),
                        "src/main.py",
                        "--input",
                        str(m2_output),
                        "--output-dir",
                        str(m1_5_dir),
                        "--database",
                        str(self.settings.project_catalog_path),
                        "--base-url",
                        self.settings.llm_base_url,
                        "--model",
                        self.settings.llm_model,
                    ],
                    cwd=self.settings.m1_5_project,
                    log_path=run_dir / "m1_5.log",
                    env=self._pipeline_environment(),
                )
                m1_5_output = m1_5_dir / "m1_5_projected_tasks.json"
                m1_5_payload = self._load_object(m1_5_output, "M1.5")
                m1_5_records = self._required_list(
                    m1_5_payload, "extract_candidates", "M1.5"
                )
                if len(m1_5_records) != len(m2_records):
                    raise PipelineError("M1.5 候选任务数量与 M2 不一致")
                self.repository.update_run(run_id, status="running_m6")

                m6_report = m6_dir / "import_report.json"
                self._run_stage(
                    [
                        str(self.settings.pipeline_python),
                        str(
                            self.settings.project_root
                            / "scripts"
                            / "import_m2_task_candidates.py"
                        ),
                        str(m1_5_output),
                        "--database",
                        str(self.settings.database_path),
                        "--output",
                        str(m6_report),
                        "--meeting-id",
                        meeting_id,
                        "--run-id",
                        run_id,
                    ],
                    cwd=self.settings.project_root,
                    log_path=run_dir / "m6.log",
                )
                report = self._load_object(m6_report, "M6")
                summary = self._import_summary(report, m2_payload, m1_5_payload)
                final_status = (
                    "completed_with_errors"
                    if summary["failed_count"]
                    else "completed"
                )
                self.repository.update_run(
                    run_id,
                    status=final_status,
                    command_count=summary["command_count"],
                    applied_count=summary["applied_count"],
                    noop_count=summary["noop_count"],
                    failed_count=summary["failed_count"],
                    summary_json=json.dumps(summary, ensure_ascii=False),
                    finished_at=utc_now(),
                )
        except Exception as exc:
            self.repository.update_run(
                run_id,
                status="failed",
                finished_at=utc_now(),
                error_summary=self._error_summary(exc),
            )
            (run_dir / "pipeline_error.log").write_text(
                traceback.format_exc(),
                encoding="utf-8",
            )

    @staticmethod
    def _error_summary(error: Exception) -> str:
        """Return a safe, actionable error for the browser-facing run record.

        Full child-process output and tracebacks belong in ``pipeline_error.log``;
        storing them in the run record would expose local paths and provider internals.
        """
        detail = str(error)
        if "模型服务暂时不可用" in detail:
            match = re.search(r"模型服务暂时不可用[^\r\n]*", detail)
            if match:
                return match.group(0)[:180]
        for status in (502, 503, 504, 500, 429):
            if str(status) in detail:
                return (
                    "模型服务暂时不可用（HTTP {}），已自动重试仍未恢复，"
                    "请稍后重新提交。"
                ).format(status)
        if isinstance(error, subprocess.TimeoutExpired) or "timeout" in detail.lower():
            return "会议材料处理超时，请缩小材料范围后重新提交。"
        if "LLM_API_KEY" in detail:
            return "模型服务未配置可用凭据，请联系管理员检查服务配置。"
        return "会议材料处理失败，请稍后重新提交；若问题持续请联系管理员。"

    def process_m2_file(
        self,
        m2_path: Path | str,
        run_id: str,
        meeting_id: str,
        meeting_date: str,
        meeting_type: str,
        output_dir: Path | str,
    ) -> dict:
        records = load_m2_records(m2_path)
        return self.automation.process_records(
            records,
            run_id=run_id,
            meeting={
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
            },
            output_dir=output_dir,
        )

    def _run_stage(
        self,
        command: list[str],
        cwd: Path,
        log_path: Path,
        env: dict[str, str] | None = None,
    ) -> None:
        if not cwd.is_dir():
            raise PipelineError(f"阶段工作目录不存在：{cwd}")
        if not Path(command[0]).is_file():
            raise PipelineError(f"阶段 Python 解释器不存在：{command[0]}")
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            env=env,
            timeout=self.settings.pipeline_timeout_seconds,
            check=False,
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "STDOUT\n"
            + result.stdout
            + "\nSTDERR\n"
            + result.stderr,
            encoding="utf-8",
        )
        if result.returncode != 0:
            raise PipelineError(
                f"阶段命令退出码 {result.returncode}："
                f"{result.stderr[-500:]}"
            )

    def _m1_environment(self) -> dict[str, str]:
        """Expose the application LLM settings to the legacy M1 CLI.

        M1 reads ``LLM_*`` names while this service is configured through
        ``M6_LLM_*``.  Without this bridge, an uploaded meeting silently
        falls back to M1's standalone provider defaults.
        """
        env = os.environ.copy()
        env.update(
            {
                "LLM_BASE_URL": self.settings.llm_base_url,
                "LLM_MODEL": self.settings.llm_model,
                "LLM_API_KEY": self.settings.llm_api_key,
            }
        )
        return env

    def _pipeline_environment(self) -> dict[str, str]:
        """Expose one configured model endpoint to every model-backed stage."""
        env = self._m1_environment()
        env.update(
            {
                "M6_LLM_BASE_URL": self.settings.llm_base_url,
                "M6_LLM_MODEL": self.settings.llm_model,
                "M6_LLM_API_KEY": self.settings.llm_api_key,
            }
        )
        return env

    @staticmethod
    def _load_object(path: Path, stage: str) -> dict:
        if not path.exists():
            raise PipelineError(f"{stage} 未生成预期产物：{path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise PipelineError(f"{stage} 产物不是 JSON 对象：{path}")
        return data

    @staticmethod
    def _required_list(payload: dict, key: str, stage: str) -> list:
        value = payload.get(key)
        if not isinstance(value, list):
            raise PipelineError(f"{stage} 产物缺少数组字段 {key}")
        return value

    def _validate_local_pipeline(self) -> None:
        requirements = {
            "M1 项目目录": self.settings.m1_project,
            "M1.5 项目目录": self.settings.m1_5_project,
            "M2 项目目录": self.settings.m2_project,
            "M1 入口": self.settings.m1_project / "src" / "main.py",
            "M2 task gate 入口": self.settings.m2_project
            / "src"
            / "m1_task_gate.py",
            "M1.5 入口": self.settings.m1_5_project / "src" / "main.py",
            "M6 导入器": self.settings.project_root
            / "scripts"
            / "import_m2_task_candidates.py",
        }
        missing = [name for name, path in requirements.items() if not path.exists()]
        if missing:
            raise PipelineError("本地新版链路配置缺失：" + "、".join(missing))
        if not self.settings.pipeline_python.is_file():
            raise PipelineError(
                f"本地新版链路 Python 不存在：{self.settings.pipeline_python}"
            )

    @staticmethod
    def _import_summary(
        report: dict, m2_payload: dict, m1_5_payload: dict
    ) -> dict:
        results = report.get("results")
        if not isinstance(results, list):
            raise PipelineError("M6 导入报告缺少 results 数组")
        statuses = [item.get("execution_status") for item in results]
        return {
            "command_count": len(results),
            "applied_count": statuses.count("applied"),
            "noop_count": statuses.count("noop") + statuses.count("duplicate"),
            "failed_count": statuses.count("failed"),
            "m2_review_count": len(m2_payload.get("review_candidates", [])),
            "m1_5_projected_count": len(
                m1_5_payload.get("extract_candidates", [])
            ),
        }
