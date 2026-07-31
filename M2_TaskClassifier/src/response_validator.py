"""解析并确定性校验 M2 模型响应。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator


LABEL_CODES = {
    "task": "T",
    "task_update": "U",
    "decision": "D",
    "issue": "I",
    "report": "R",
    "suggestion": "S",
    "non_task": "N",
}
CANDIDATE_LABELS = {"task", "task_update", "decision", "issue"}
ACTION_WORDS = (
    "完成", "提交", "整改", "检查", "复核", "落实", "解决", "推进", "汇报",
    "处理", "维修", "检修", "更换", "组织", "安排", "制定", "审核", "排查",
)
RESPONSIBILITY_WORDS = ("由", "负责", "牵头", "配合", "安监科", "机电队", "责任人")
TIME_WORDS = ("今天", "明天", "本周", "周一", "周二", "周三", "周四", "周五", "月底", "之前", "以内", "前")
STATUS_WORDS = ("已完成", "延期", "取消", "暂停", "受阻", "改为", "改由", "不再执行")
SAFETY_WORDS = ("安全", "隐患", "事故", "应急", "停机", "停产", "瓦斯", "火灾", "爆炸", "高风险")


class ResponseValidationError(ValueError):
    """模型响应不满足 M2 数据契约。"""


def load_schema(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _extract_object_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    return text


def _normalize_ambiguities(result: dict) -> tuple[dict, bool]:
    """兼容模型沿用 M1 text/reason 结构，只做无损字段归一化。"""
    ambiguities = result.get("ambiguities")
    if not isinstance(ambiguities, list):
        return result, False
    original = copy.deepcopy(ambiguities)
    normalized = []
    for item in ambiguities:
        if isinstance(item, str) and item.strip():
            normalized.append({"field": "content", "description": item.strip()})
            continue
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        if set(item) == {"field", "description"}:
            normalized.append(item)
            continue
        text = item.get("text")
        reason = item.get("reason") or item.get("description")
        if text and reason:
            description = f"{text}：{reason}"
        else:
            description = reason or text
        if description:
            normalized.append(
                {"field": item.get("field") or "content", "description": description}
            )
        else:
            normalized.append(item)
    result["ambiguities"] = normalized
    return result, normalized != original


def _unique_span(text: str, evidence_text: str) -> tuple[int, int] | None:
    """只在精确或仅空白差异且唯一匹配时恢复原文区间。"""
    start = text.find(evidence_text)
    if start >= 0 and text.find(evidence_text, start + 1) < 0:
        return start, start + len(evidence_text)

    compact_evidence = "".join(char for char in evidence_text if not char.isspace())
    if not compact_evidence:
        return None
    compact_text_chars = []
    source_indexes = []
    for index, char in enumerate(text):
        if not char.isspace():
            compact_text_chars.append(char)
            source_indexes.append(index)
    compact_text = "".join(compact_text_chars)
    compact_start = compact_text.find(compact_evidence)
    if compact_start < 0 or compact_text.find(compact_evidence, compact_start + 1) >= 0:
        return None
    compact_end = compact_start + len(compact_evidence) - 1
    return source_indexes[compact_start], source_indexes[compact_end] + 1


def parse_and_validate_response(
    content: str,
    schema: dict,
    segment_id: str,
    current_text: str,
    include_normalizations: bool = False,
) -> dict | tuple[dict, list[str]]:
    """校验 JSON Schema、标签映射、字段顺序与证据字符区间。"""
    try:
        result = json.loads(_extract_object_text(content))
    except json.JSONDecodeError as exc:
        raise ResponseValidationError(f"JSON 解析失败：{exc.msg}") from exc
    if not isinstance(result, dict):
        raise ResponseValidationError("M2 响应必须是单个 JSON 对象")
    result, ambiguities_normalized = _normalize_ambiguities(result)
    normalizations = []
    if ambiguities_normalized:
        normalizations.append("ambiguity_fields_normalized")

    item_schema = schema["items"]
    errors = sorted(
        Draft202012Validator(item_schema).iter_errors(result),
        key=lambda error: list(error.path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "<root>"
        raise ResponseValidationError(f"Schema 校验失败 {location}：{first.message}")

    if next(iter(result)) != "label_code":
        raise ResponseValidationError("label_code 必须是 JSON 对象的第一个字段")
    if result["segment_id"] != segment_id:
        raise ResponseValidationError("segment_id 与输入片段不一致")
    expected_code = LABEL_CODES[result["primary_label"]]
    if result["label_code"] != expected_code:
        raise ResponseValidationError("label_code 与 primary_label 不匹配")
    if result["primary_label"] in result["secondary_labels"]:
        raise ResponseValidationError("secondary_labels 不得重复 primary_label")

    for index, evidence in enumerate(result["evidence"]):
        start = evidence["start_char"]
        end = evidence["end_char"]
        if end <= start or end > len(current_text) or current_text[start:end] != evidence["text"]:
            recovered = _unique_span(current_text, evidence["text"])
            if recovered is None:
                raise ResponseValidationError(
                    f"evidence[{index}] 不是 current_segment.clean_text 中可唯一恢复的原文"
                )
            recovered_start, recovered_end = recovered
            evidence["start_char"] = recovered_start
            evidence["end_char"] = recovered_end
            evidence["text"] = current_text[recovered_start:recovered_end]
            normalizations.append(f"evidence_offset_recovered:{index}")
    if include_normalizations:
        return result, normalizations
    return result


def enforce_recall_routes(result: dict, current_text: str) -> tuple[dict, list[str]]:
    """弱规则只提升候选路由，不直接创建正式任务或改写主标签。"""
    guarded = copy.deepcopy(result)
    overrides: list[str] = []
    signals = guarded["signals"]
    has_action = signals["explicit_action"] or any(word in current_text for word in ACTION_WORDS)
    has_responsibility = signals["explicit_actor"] or any(
        word in current_text for word in RESPONSIBILITY_WORDS
    )
    has_time = signals["explicit_deadline"] or any(word in current_text for word in TIME_WORDS)
    has_status = any(word in current_text for word in STATUS_WORDS)
    safety_related = signals["safety_related"] or any(
        word in current_text for word in SAFETY_WORDS
    )
    if safety_related and not signals["safety_related"]:
        signals["safety_related"] = True
        overrides.append("safety_keyword_signal")

    label = guarded["primary_label"]
    route = guarded["route"]
    if label in CANDIDATE_LABELS and route not in {"EXTRACT", "HUMAN_REVIEW"}:
        guarded["route"] = "EXTRACT"
        overrides.append("candidate_label_recalled")
    elif label == "report" and has_action and (has_responsibility or has_time or has_status):
        if route not in {"EXTRACT", "HUMAN_REVIEW"}:
            guarded["route"] = "EXTRACT"
            overrides.append("actionable_report_recalled")
    elif label in {"suggestion", "non_task"} and has_action and (
        has_responsibility or has_time or has_status
    ):
        if route == "DISCARD":
            guarded["route"] = "HUMAN_REVIEW"
            overrides.append("weak_task_signal_review")

    if safety_related and guarded["route"] in {"DISCARD", "CONTEXT_ONLY"}:
        guarded["route"] = "HUMAN_REVIEW"
        overrides.append("safety_candidate_review")
    return guarded, overrides


def validate_batch(results: list[dict], schema: dict) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(results),
        key=lambda error: list(error.path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "<root>"
        raise ResponseValidationError(f"M2 批量输出校验失败 {location}：{first.message}")
