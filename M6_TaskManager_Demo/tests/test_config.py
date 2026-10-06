from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from app.config import Settings


class SettingsTest(unittest.TestCase):
    def test_defaults_only_configure_results_and_review(self) -> None:
        project_root = Path(__file__).resolve().parent.parent
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.load(project_root=project_root)
        self.assertEqual(settings.database_path, project_root / "data/demo.db")
        self.assertEqual(settings.runs_dir, project_root / "demo_runs")
        self.assertEqual(settings.static_dir, project_root / "static")
        self.assertFalse(settings.seed_demo_data)
        self.assertEqual(
            {field.name for field in fields(Settings)},
            {
                "project_root", "database_path", "runs_dir", "static_dir",
                "llm_base_url", "llm_model", "llm_api_key",
                "llm_timeout_seconds", "seed_demo_data",
            },
        )

    def test_environment_selects_display_database_and_review_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    "M6_DATABASE_PATH": str(root / "results.sqlite"),
                    "M6_LLM_BASE_URL": "http://127.0.0.1:8000/v1",
                    "M6_LLM_MODEL": "review-model",
                    "M6_LLM_TIMEOUT_SECONDS": "45",
                    "M6_SEED_DEMO_DATA": "true",
                },
                clear=True,
            ):
                settings = Settings.load(project_root=root)
        self.assertEqual(settings.database_path, root / "results.sqlite")
        self.assertEqual(settings.llm_base_url, "http://127.0.0.1:8000/v1")
        self.assertEqual(settings.llm_model, "review-model")
        self.assertEqual(settings.llm_timeout_seconds, 45)
        self.assertTrue(settings.seed_demo_data)


if __name__ == "__main__":
    unittest.main()
