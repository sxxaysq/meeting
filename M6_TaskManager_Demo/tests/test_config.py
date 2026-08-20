from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.orchestrator import PipelineOrchestrator


class SettingsTest(unittest.TestCase):
    def test_pipeline_error_summary_is_safe_and_actionable(self) -> None:
        summary = PipelineOrchestrator._error_summary(
            RuntimeError("stage failed: openai.InternalServerError: Error code: 502")
        )
        self.assertEqual(
            summary,
            "模型服务暂时不可用（HTTP 502），已自动重试仍未恢复，请稍后重新提交。",
        )
        generic = PipelineOrchestrator._error_summary(
            RuntimeError("Traceback: C:\\secret\\internal.py")
        )
        self.assertEqual(
            generic,
            "会议材料处理失败，请稍后重新提交；若问题持续请联系管理员。",
        )

    def test_default_paths_target_local_new_pipeline(self) -> None:
        project_root = Path(__file__).resolve().parent.parent
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.load(project_root=project_root)
        self.assertEqual(settings.m1_project, project_root.parent / "M1_Extraction")
        self.assertEqual(
            settings.m2_project, project_root.parent / "M2_SemanticConsolidator"
        )
        self.assertEqual(settings.m6_project, project_root.parent / "M6_TaskManager")
        self.assertNotEqual(settings.database_path, settings.lifecycle_database_path)
        self.assertFalse(settings.seed_demo_data)

    def test_new_m6_summary(self) -> None:
        summary = PipelineOrchestrator._import_summary(
            {
                "records": [{"model_audit": {}}, {"model_audit": {}}],
                "execution_status_counts": {"APPLIED": 1, "SKIPPED": 1},
                "action_counts": {"CREATE": 1, "SKIP": 1},
            },
            {"validation": {"status": "REVIEW"}},
        )
        self.assertEqual(summary["command_count"], 2)
        self.assertEqual(summary["applied_count"], 1)
        self.assertEqual(summary["noop_count"], 1)
        self.assertEqual(summary["m2_validation_status"], "REVIEW")

    def test_pipeline_python_keeps_virtualenv_symlink_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            system_python.write_text("", encoding="utf-8")
            venv_python = root / "venv-python"
            try:
                venv_python.symlink_to(system_python)
            except OSError:
                self.skipTest("当前文件系统不允许创建符号链接")
            with patch.dict(
                os.environ,
                {"M6_PIPELINE_PYTHON": str(venv_python)},
                clear=False,
            ):
                settings = Settings.load(project_root=root)
            self.assertEqual(settings.pipeline_python, venv_python)
            self.assertNotEqual(settings.pipeline_python, system_python)

    def test_m1_receives_application_llm_settings(self) -> None:
        with patch.dict(os.environ, {"KEEP_ME": "present"}, clear=True):
            settings = Settings.load(project_root=Path(__file__).resolve().parent.parent)
            environment = PipelineOrchestrator(
                settings=settings,
                repository=None,
            )
            environment = environment._m1_environment()
        self.assertEqual(environment["LLM_BASE_URL"], settings.llm_base_url)
        self.assertEqual(environment["LLM_MODEL"], settings.llm_model)
        self.assertEqual(environment["LLM_API_KEY"], settings.llm_api_key)
        self.assertEqual(environment["KEEP_ME"], "present")


if __name__ == "__main__":
    unittest.main()
