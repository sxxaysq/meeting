"""Load only validated M2 outputs and attach stable source identities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from .identifiers import source_document_id, source_item_id
from .models import ALL_ITEM_TYPES, InputContractError, SourceContext


def _schema_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "M2_SemanticConsolidator"
        / "schemas"
        / "m2_output.schema.json"
    )


def load_m2_payload(
    path: Path | str,
    *,
    document_id: str | None = None,
    require_pass: bool = True,
) -> tuple[str, list[SourceContext]]:
    input_path = Path(path)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise InputContractError("M2 输出必须是 JSON 对象")

    schema_file = _schema_path()
    if not schema_file.exists():
        raise InputContractError("M2 Schema 文件缺失，拒绝跳过校验")
    if schema_file.exists():
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        errors = sorted(
            Draft7Validator(schema).iter_errors(payload),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            details = "; ".join(
                f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: "
                f"{error.message}"
                for error in errors[:5]
            )
            raise InputContractError(f"M2 Schema 校验失败：{details}")

    validation = payload.get("validation") or {}
    from .input_policy import HARD_CODES
    if validation.get("status") == "ERROR" or any(
        issue.get("level") == "error" or issue.get("code") in HARD_CODES
        for issue in validation.get("issues", [])
    ):
        raise InputContractError("M2 硬错误：拒绝整份输入")
    if require_pass and validation.get("status") != "PASS":
        raise InputContractError(
            "M6 默认只接受 M2 validation.status=PASS 的输出；"
            f"当前为 {validation.get('status')!r}"
        )
    items = payload.get("items")
    traces = payload.get("merge_trace")
    if not isinstance(items, list) or not isinstance(traces, list):
        raise InputContractError("M2 输出缺少 items/merge_trace 数组")
    if validation.get("status") == "REVIEW" and not any(i['level'] == 'review' for i in validation['issues']):
        raise InputContractError("M2 REVIEW 缺少复核原因")
    trace_by_index = {trace.get("item_index"): trace for trace in traces}
    if len(traces) != len(items) or set(trace_by_index) != set(range(len(items))):
        raise InputContractError("merge_trace 未逐条覆盖 items")
    all_sources = []
    for index, item in enumerate(items):
        trace = trace_by_index[index]
        sources, evidence = trace['source_indexes'], trace['source_evidence']
        if (not sources or len(sources) != len(evidence)
                or any(type(i) is not int or i < 0 for i in sources)
                or trace['merged'] != (len(sources) > 1)):
            raise InputContractError("来源索引/证据缺失或不一致")
        all_sources.extend(sources)
        for span in [item['evidence'], *evidence]:
            if not span.get('text', '').strip() or span['start_char'] > span['end_char']:
                raise InputContractError("来源证据为空或坐标倒置")
            if span.get('page_start') is not None and span.get('page_end') is not None and span['page_start'] > span['page_end']:
                raise InputContractError("来源页码倒置")
    if sorted(all_sources) != list(range(len(all_sources))):
        raise InputContractError("来源覆盖存在缺口或重复")
    for issue in validation['issues']:
        for key, container in [('item_index', issue), ('other_item_index', issue.get('detail', {}))]:
            if key in container and (type(container[key]) is not int or not 0 <= container[key] < len(items)):
                raise InputContractError("告警条目索引非法")
        scope = issue.get('detail', {}).get('item_indexes')
        if scope is not None and (not isinstance(scope, list) or any(type(i) is not int or not 0 <= i < len(items) for i in scope)):
            raise InputContractError("告警条目索引非法")

    resolved_document_id = source_document_id(
        payload,
        input_path=input_path,
        explicit_id=document_id,
    )
    project_entities = payload.get("project_entities") or []
    contexts: list[SourceContext] = []
    for index, item in enumerate(items):
        if item.get("item_type") not in ALL_ITEM_TYPES:
            raise InputContractError(f"items[{index}].item_type 无效")
        trace = trace_by_index[index]
        contexts.append(
            SourceContext(
                source_document_id=resolved_document_id,
                source_item_id=source_item_id(
                    resolved_document_id,
                    index,
                    item,
                    trace,
                ),
                source_mode=str(payload["source_mode"]),
                item_index=index,
                item=item,
                merge_trace=trace,
                project_entity_id=trace.get("project_entity_id"),
                project_entities=project_entities,
                m2_validation=validation,
            )
        )
    return resolved_document_id, contexts


def context_from_payload(
    payload: dict[str, Any],
    *,
    document_id: str,
    item_index: int,
) -> SourceContext:
    """Small in-memory adapter used by tests and embedding applications."""
    traces = {
        trace["item_index"]: trace for trace in payload.get("merge_trace", [])
    }
    item = payload["items"][item_index]
    trace = traces[item_index]
    return SourceContext(
        source_document_id=document_id,
        source_item_id=source_item_id(document_id, item_index, item, trace),
        source_mode=payload["source_mode"],
        item_index=item_index,
        item=item,
        merge_trace=trace,
        project_entity_id=trace.get("project_entity_id"),
        project_entities=payload.get("project_entities", []),
        m2_validation=payload["validation"],
    )
