"""应用配置，仅从环境变量读取可变部署参数。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    project_root: Path
    database_path: Path
    runs_dir: Path
    static_dir: Path
    m1_project: Path
    project_catalog_path: Path
    m2_project: Path
    m6_project: Path
    lifecycle_database_path: Path
    departments_path: Path
    pipeline_python: Path
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout_seconds: float
    pipeline_timeout_seconds: int
    max_upload_bytes: int
    max_task_candidates: int
    seed_demo_data: bool
    allow_decision_create: bool

    @classmethod
    def load(cls, project_root: Path | None = None) -> "Settings":
        root = (project_root or Path(__file__).resolve().parent.parent).resolve()
        data_dir = root / "data"
        local_m1 = root.parent / "M1_Extraction"
        local_m2 = root.parent / "M2_SemanticConsolidator"
        local_m6 = root.parent / "M6_TaskManager"
        windows_venv_python = root / ".venv" / "Scripts" / "python.exe"
        unix_venv_python = root / ".venv" / "bin" / "python"
        default_python = (
            windows_venv_python
            if windows_venv_python.exists()
            else unix_venv_python
            if unix_venv_python.exists()
            else Path(sys.executable)
        )
        database_path = Path(
            os.getenv("M6_DATABASE_PATH", str(data_dir / "demo.db"))
        ).resolve()
        project_catalog_path = Path(
            os.getenv(
                "M6_PROJECT_CATALOG_PATH", str(data_dir / "project_catalog.db")
            )
        ).resolve()
        return cls(
            project_root=root,
            database_path=database_path,
            runs_dir=Path(
                os.getenv("M6_RUNS_DIR", str(root / "demo_runs"))
            ).resolve(),
            static_dir=Path(
                os.getenv("M6_STATIC_DIR", str(root / "static"))
            ).resolve(),
            m1_project=Path(
                os.getenv("M6_M1_PROJECT", str(local_m1))
            ).resolve(),
            project_catalog_path=project_catalog_path,
            m2_project=Path(
                os.getenv("M6_M2_PROJECT", str(local_m2))
            ).resolve(),
            m6_project=Path(
                os.getenv("M6_M6_PROJECT", str(local_m6))
            ).resolve(),
            lifecycle_database_path=Path(
                os.getenv(
                    "M6_LIFECYCLE_DATABASE_PATH",
                    str(data_dir / "lifecycle.db"),
                )
            ).resolve(),
            departments_path=Path(
                os.getenv(
                    "M6_DEPARTMENTS_PATH",
                    str(local_m6 / "db" / "departments.frontend.json"),
                )
            ).resolve(),
            # 不调用 resolve()：虚拟环境的 python 通常是符号链接，解析后会
            # 退化为系统解释器并丢失该 venv 的 site-packages。
            pipeline_python=Path(
                os.getenv("M6_PIPELINE_PYTHON", str(default_python))
            ).expanduser(),
            llm_base_url=os.getenv(
                "M6_LLM_BASE_URL", "http://192.168.30.215:8000/v1"
            ),
            llm_model=os.getenv(
                "M6_LLM_MODEL", "Qwen/Qwen3.6-35B-A3B"
            ),
            llm_api_key=os.getenv("M6_LLM_API_KEY", "EMPTY"),
            llm_timeout_seconds=float(
                os.getenv("M6_LLM_TIMEOUT_SECONDS", "120")
            ),
            pipeline_timeout_seconds=int(
                os.getenv("M6_PIPELINE_TIMEOUT_SECONDS", "1800")
            ),
            max_upload_bytes=int(
                os.getenv("M6_MAX_UPLOAD_BYTES", str(30 * 1024 * 1024))
            ),
            max_task_candidates=int(
                os.getenv("M6_MAX_TASK_CANDIDATES", "5")
            ),
            seed_demo_data=_env_bool("M6_SEED_DEMO_DATA", False),
            allow_decision_create=_env_bool(
                "M6_ALLOW_DECISION_CREATE", False
            ),
        )
