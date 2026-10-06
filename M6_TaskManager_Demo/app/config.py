"""应用配置，仅从环境变量读取可变部署参数。"""

from __future__ import annotations

import os
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
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout_seconds: float
    seed_demo_data: bool

    @classmethod
    def load(cls, project_root: Path | None = None) -> "Settings":
        root = (project_root or Path(__file__).resolve().parent.parent).resolve()
        data_dir = root / "data"
        database_path = Path(
            os.getenv("M6_DATABASE_PATH", str(data_dir / "demo.db"))
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
            seed_demo_data=_env_bool("M6_SEED_DEMO_DATA", False),
        )
