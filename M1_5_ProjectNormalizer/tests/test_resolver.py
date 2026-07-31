from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from main import project_m2_payload
from project_catalog import ProjectCatalog
from resolver import ProjectResolver


class ScriptedModel:
    def __init__(self, results: list[dict]) -> None:
        self.results = iter(results)
        self.catalogs: list[list[dict]] = []

    def resolve(self, text: str, source_name: str | None, catalog: list[dict]) -> dict:
        del text, source_name
        self.catalogs.append(catalog)
        return next(self.results)


class ProjectResolverTest(unittest.TestCase):
    def test_aliases_share_one_project_but_keep_source_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProjectCatalog(Path(directory) / "tasks.db")
            model = ScriptedModel(
                [
                    {
                        "resolution": "new",
                        "canonical_name": "延长项目",
                        "confidence": 0.93,
                    },
                ]
            )
            resolver = ProjectResolver(catalog, model)
            first = resolver.resolve_record(
                {
                    "task_text": "延长矿调度中心项目：完成联调。",
                    "project_anchor": "延长矿调度中心项目",
                }
            )
            follow_up = ScriptedModel(
                [
                    {
                        "resolution": "existing",
                        "project_id": first["project_id"],
                        "confidence": 0.96,
                    }
                ]
            )
            second = ProjectResolver(catalog, follow_up).resolve_record(
                {
                    "task_text": "延长矿业项目：完成验收资料。",
                    "project_anchor": "延长矿业项目",
                }
            )
            self.assertEqual(first["project_id"], second["project_id"])
            self.assertEqual(first["canonical_name"], "延长项目")
            self.assertEqual(second["source_name"], "延长矿业项目")
            self.assertEqual(len(follow_up.catalogs[0]), 1)
            self.assertEqual(
                set(catalog.list_for_model()[0]["aliases"]),
                {"延长项目", "延长矿调度中心项目", "延长矿业项目"},
            )

    def test_no_explicit_project_creates_no_master_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProjectCatalog(Path(directory) / "tasks.db")
            resolver = ProjectResolver(catalog, ScriptedModel([]))
            context = resolver.resolve_record(
                {"task_text": "通风队明天提交风量测定方案。", "project_anchor": None}
            )
            self.assertEqual(context["resolution"], "none")
            self.assertEqual(catalog.list_for_model(), [])

    def test_m2_payload_reuses_space_variant_without_changing_task_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProjectCatalog(Path(directory) / "projects.db")
            first = project_m2_payload(
                {
                    "schema_version": "m2.task_gate.v2",
                    "extract_candidates": [
                        {
                            "candidate_id": "m1-001",
                            "project": "CRM二期项目",
                            "title": "保留原始小项标题",
                            "source_text": "项目：CRM二期项目\n任务：保留原始小项标题",
                        }
                    ],
                },
                ProjectResolver(
                    catalog,
                    ScriptedModel(
                        [
                            {
                                "resolution": "new",
                                "canonical_name": "CRM二期项目",
                            }
                        ]
                    ),
                ),
            )
            reused_model = ScriptedModel([])
            second = project_m2_payload(
                {
                    "schema_version": "m2.task_gate.v2",
                    "extract_candidates": [
                        {
                            "candidate_id": "m1-002",
                            "project": "CRM 二期项目",
                            "title": "另一条原始小项标题",
                            "source_text": "项目：CRM 二期项目\n任务：另一条原始小项标题",
                        }
                    ],
                },
                ProjectResolver(catalog, reused_model),
            )
            first_item = first["extract_candidates"][0]
            second_item = second["extract_candidates"][0]
            self.assertEqual(first_item["project"], "CRM二期项目")
            self.assertEqual(second_item["project"], "CRM二期项目")
            self.assertEqual(second_item["title"], "另一条原始小项标题")
            self.assertEqual(second_item["project_context"]["source_name"], "CRM 二期项目")
            self.assertEqual(reused_model.catalogs, [])

    def test_model_new_project_variant_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProjectCatalog(Path(directory) / "projects.db")
            context = ProjectResolver(
                catalog,
                ScriptedModel(
                    [
                        {
                            "resolution": "new_project",
                            "canonical_name": "ERP项目",
                        }
                    ]
                ),
            ).resolve_record(
                {"task_text": "ERP项目：完成上线。", "project_anchor": "ERP 项目"}
            )
            self.assertEqual(context["resolution"], "new")
            self.assertEqual(context["canonical_name"], "ERP项目")


if __name__ == "__main__":
    unittest.main()
