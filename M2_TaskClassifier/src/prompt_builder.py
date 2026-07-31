"""构造带明确数据边界的 M2 分类提示词。"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any


def load_system_prompt(path: Path | str) -> str:
    """读取固定系统提示词。"""
    return Path(path).read_text(encoding="utf-8").strip()


def _xml(value: Any) -> str:
    if value is None or value == "":
        return "UNKNOWN"
    return escape(str(value), quote=True)


def _json_xml(value: Any) -> str:
    return _xml(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def build_user_prompt(
    record: dict,
    context_before: str,
    context_after: str,
) -> str:
    """把一个 M1 片段及相邻上下文转换为 M2 输入。"""
    clean_text = record.get("clean_text") or record.get("raw_text") or ""
    return f"""请分类下面的一个 M1 片段，只输出 JSON 对象。

<meeting>
meeting_title: {_xml(record.get("meeting_title"))}
meeting_date: UNKNOWN
meeting_type: UNKNOWN
speaker_id: {_xml(record.get("speaker_id"))}
speaker_name: {_xml(record.get("speaker_name"))}
speaker_role: {_xml(record.get("speaker_role"))}
speaker_department: {_xml(record.get("speaker_department"))}
timestamp: {_xml(record.get("timestamp"))}
</meeting>

<context_before>
{_xml(context_before)}
</context_before>

<current_segment id="{_xml(record.get('segment_id'))}">
<clean_text>{_xml(clean_text)}</clean_text>
<raw_text>{_xml(record.get("raw_text"))}</raw_text>
</current_segment>

<context_after>
{_xml(context_after)}
</context_after>

<asr_info>
quality_flags: {_json_xml(record.get("quality_flags", []))}
uncertainties: {_json_xml(record.get("uncertainties", []))}
asr_confidence: {_xml(record.get("asr_confidence"))}
nbest: {_json_xml(record.get("nbest", []))}
</asr_info>

证据下标必须相对于 current_segment.clean_text，segment_id 必须原样输出。"""


def build_repair_prompt(
    original_prompt: str,
    invalid_response: str,
    validation_error: str,
) -> str:
    """构造唯一一次格式/证据修复请求，不引入新的业务事实。"""
    return f"""上一次输出未通过确定性校验。请仅修复 JSON、枚举、字段或证据下标，不得改变输入事实，也不得输出解释。

<validation_error>
{_xml(validation_error)}
</validation_error>

<invalid_response>
{_xml(invalid_response)}
</invalid_response>

<original_request>
{_xml(original_prompt)}
</original_request>"""
