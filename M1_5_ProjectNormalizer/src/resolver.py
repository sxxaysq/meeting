"""M1.5：以项目主表为事实来源的 LLM 项目归并。"""

from __future__ import annotations

import json
import re
from typing import Protocol

from project_catalog import ProjectCatalog


PROJECT_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{2,40}(?:项目|平台|系统|中心)"
)

SYSTEM_PROMPT = """你是煤矿会议项目归并器。只输出 JSON 对象。
项目主表是唯一可复用的项目事实：若当前原始项目称谓属于表中已有大项目，必须输出该项目的 project_id 和 canonical_name；不要创建同义新项目。
项目归并只标注归属，绝不改写当前 task_text 或子项任务名称。若无明确项目称谓，输出 resolution=none 且其余字段为 null。
若主表没有该项目，输出 resolution=new、一个简洁稳定的 canonical_name，以及原文 source_name。不得因名称相似把不相关项目合并。"""


class ProjectModel(Protocol):
    def resolve(self, text: str, source_name: str | None, catalog: list[dict]) -> dict:
        ...


class OpenAICompatibleProjectModel:
    def __init__(self, base_url: str, model: str, api_key: str = "EMPTY") -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key or "EMPTY", base_url=base_url, timeout=120)
        self.model = model

    def resolve(self, text: str, source_name: str | None, catalog: list[dict]) -> dict:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"task_text": text, "source_name": source_name, "project_catalog": catalog},
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content or "{}")
        if not isinstance(result, dict):
            raise ValueError("M1.5 模型输出必须为对象")
        return result


def infer_source_name(record: dict) -> str | None:
    anchor = record.get("project_anchor")
    if isinstance(anchor, str) and anchor.strip():
        return anchor.strip()
    match = PROJECT_PATTERN.search(record.get("task_text", ""))
    return match.group(0) if match else None


def _none() -> dict:
    return {
        "project_id": None,
        "canonical_name": None,
        "source_name": None,
        "resolution": "none",
        "confidence": 1.0,
    }


def _normalize_resolution(result: dict) -> str:
    """Accept harmless model spelling variants without inventing a project link."""
    value = str(result.get("resolution") or "").strip().lower()
    aliases = {
        "existing": "existing",
        "exist": "existing",
        "match": "existing",
        "matched": "existing",
        "reuse": "existing",
        "existing_project": "existing",
        "new": "new",
        "create": "new",
        "new_project": "new",
        "none": "none",
        "no_project": "none",
        "unknown": "none",
    }
    resolved = aliases.get(value)
    if resolved is not None:
        return resolved
    if result.get("project_id"):
        return "existing"
    if isinstance(result.get("canonical_name"), str) and result["canonical_name"].strip():
        return "new"
    return "none"


class ProjectResolver:
    def __init__(self, catalog: ProjectCatalog, model: ProjectModel) -> None:
        self.catalog = catalog
        self.model = model

    def resolve_record(self, record: dict) -> dict:
        source_name = infer_source_name(record)
        if source_name is None:
            return _none()
        known = self.catalog.resolve_source_name(source_name)
        if known is not None:
            return {
                **known,
                "source_name": source_name,
                "resolution": "existing",
                "confidence": 1.0,
            }
        result = self.model.resolve(
            record["task_text"], source_name, self.catalog.list_for_model()
        )
        resolution = _normalize_resolution(result)
        if resolution == "existing":
            existing = self.catalog.resolve_existing(str(result.get("project_id") or ""))
            if existing is None:
                raise ValueError("M1.5 只能引用项目主表中存在的 project_id")
            linked = self.catalog.create_or_add_alias(
                existing["canonical_name"], source_name
            )
            return {
                **linked,
                "source_name": source_name,
                "resolution": "existing",
                "confidence": float(result.get("confidence", 1.0)),
            }
        if resolution == "new":
            canonical_name = result.get("canonical_name")
            if not isinstance(canonical_name, str) or not canonical_name.strip():
                raise ValueError("M1.5 新项目必须提供 canonical_name")
            linked = self.catalog.create_or_add_alias(canonical_name, source_name)
            return {
                **linked,
                "source_name": source_name,
                "resolution": "new",
                "confidence": float(result.get("confidence", 1.0)),
            }
        if resolution == "none":
            return _none()
        raise ValueError("M1.5 resolution 归一化失败")
