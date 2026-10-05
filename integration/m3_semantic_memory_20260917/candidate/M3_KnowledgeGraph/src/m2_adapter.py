"""M2 Adapter：M3 的唯一入口，只做"验证 + 门禁"，绝不重新解释 M2 的事实。

职责：
1. 读取 M2 输出 JSON；
2. 按 M2 官方 schema（schemas/m2_output.schema.json 的冻结副本）做结构校验；
3. 质量门禁：validation.status != PASS 一律拒绝，除非显式 force；
4. 校验 meeting_date / source_document_id 元数据。

禁止在此做：alias 判断、Item 合并、项目归一、部门识别 —— 这些是 M2 的结论。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

from M3_KnowledgeGraph.src.models import M2AdapterError, M2RejectedError

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATE_SEARCH_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _load_schema(name: str) -> dict:
    path = SCHEMA_DIR / name
    if not path.exists():
        raise M2AdapterError(f"缺少 schema 文件: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_m2_output(path: str | Path) -> dict:
    """读取并结构校验 M2 输出，返回原始 dict（不做任何改写）。"""
    p = Path(path)
    if not p.exists():
        raise M2AdapterError(f"M2 输出文件不存在: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise M2AdapterError(f"M2 输出不是合法 JSON: {p}: {e}") from e

    schema = _load_schema("m2_output.schema.json")
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        raise M2AdapterError(f"M2 输出不符合 m2_output.schema.json: {e.message} (path: {list(e.absolute_path)})") from e
    return data


def check_quality_gate(data: dict, force: bool = False) -> str:
    """质量门禁。返回 ingest_mode：formal / forced；非法状态抛 M2RejectedError。"""
    status = data.get("validation", {}).get("status")
    if status == "PASS":
        return "formal"
    if not force:
        raise M2RejectedError(
            f"M2 validation.status={status}，默认拒绝入图（仅 PASS 可正式入图）。"
            f"如确需实验性导入请加 --force（所有节点将打 m3_ingest_mode=forced 标记）。"
        )
    return "forced"


def derive_meeting_date(path: str | Path) -> str | None:
    """从文件名派生会议日期（如 2026-04-07.m2.json → 2026-04-07）。不猜内容里的日期。

    只扫描文件名中的 YYYY-MM-DD 子串（兼容 .m2.json 等多层后缀）。"""
    m = _DATE_SEARCH_RE.search(Path(path).name)
    return m.group(1) if m else None


def validate_meeting_date(value: str) -> str:
    if not _DATE_RE.match(value):
        raise M2AdapterError(f"meeting_date 必须为 YYYY-MM-DD 格式，实际: {value!r}")
    y, mo, d = (int(x) for x in value.split("-"))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        raise M2AdapterError(f"meeting_date 非法: {value!r}")
    return value


def _envelope_validator():
    """构建带 Registry 的信封校验器：graph_input.schema.json 通过 $ref 引用独立的
    m2_output.schema.json（二者各自带 $id），避免内嵌 $ref 解析错乱。"""
    m2_schema = _load_schema("m2_output.schema.json")
    env_schema = _load_schema("graph_input.schema.json")
    registry = Registry().with_resources(
        [
            (m2_schema["$id"], Resource.from_contents(m2_schema)),
            (env_schema["$id"], Resource.from_contents(env_schema)),
        ]
    )
    validator_cls = jsonschema.validators.validator_for(env_schema)
    validator_cls.check_schema(env_schema)
    return validator_cls(env_schema, registry=registry)


def build_envelope(
    m2_output: dict,
    source_document_id: str,
    meeting_date: str,
) -> dict:
    """组装 M3 输入信封并用 graph_input.schema.json 校验。"""
    envelope = {
        "m2_output": m2_output,
        "source_document_id": source_document_id,
        "meeting_date": validate_meeting_date(meeting_date),
    }
    validator = _envelope_validator()
    errors = sorted(validator.iter_errors(envelope), key=lambda e: list(e.absolute_path))
    if errors:
        e = errors[0]
        raise M2AdapterError(
            f"M3 输入信封不符合 graph_input.schema.json: {e.message} (path: {list(e.absolute_path)})"
        )
    return envelope
