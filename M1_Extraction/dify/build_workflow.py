# -*- coding: utf-8 -*-
"""生成 Dify Workflow DSL。

Code 节点的 Python 不直接写在 YAML 里，而是从 dify/code_nodes/*.py 内联，
这样它们可以被 pytest 直接测试（见 tests/test_dify_nodes.py），
不会出现"YAML 里的代码没人测过"的情况。

用法::

    python dify/build_workflow.py                      # 默认 Ollama
    python dify/build_workflow.py --provider langgenius/openai_api_compatible/openai_api_compatible \
                                  --model qwen3:30b -o dify/m1_workflow.yml
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = Path(__file__).resolve().parent / "code_nodes"
PROMPTS = ROOT / "prompts"


def load_code(name: str) -> str:
    return (CODE_DIR / name).read_text(encoding="utf-8")


def split_prompt(text: str):
    """把 item_extractor.md 拆成 system 规则段与 user 上下文段。"""
    marker = "## Block 上下文"
    head, _, tail = text.partition(marker)
    return head.strip(), (marker + tail).strip()


def code_node(node_id, title, code, outputs, variables, position, desc="", parent=None):
    data = {
        "code": code,
        "code_language": "python3",
        "desc": desc,
        "outputs": {k: {"children": None, "type": v} for k, v in outputs.items()},
        "selected": False,
        "title": title,
        "type": "code",
        "variables": [
            {"value_selector": selector, "variable": name} for name, selector in variables
        ],
    }
    node = {
        "data": data,
        "height": 66,
        "id": node_id,
        "position": position,
        "positionAbsolute": position,
        "selected": False,
        "sourcePosition": "right",
        "targetPosition": "left",
        "type": "custom",
        "width": 244,
    }
    if parent:
        data["isInIteration"] = True
        data["iteration_id"] = parent
        node["parentId"] = parent
        node["extent"] = "parent"
        node["zIndex"] = 1001
        node["positionAbsolute"] = {"x": position["x"] + 930, "y": position["y"] + 200}
    return node


def edge(source, target, source_type, target_type, in_iteration=False, parent=None):
    data = {
        "isInIteration": in_iteration,
        "sourceType": source_type,
        "targetType": target_type,
    }
    if in_iteration and parent:
        data["iteration_id"] = parent
    result = {
        "data": data,
        "id": "{}-source-{}-target".format(source, target),
        "source": source,
        "sourceHandle": "source",
        "target": target,
        "targetHandle": "target",
        "type": "custom",
        "zIndex": 1002 if in_iteration else 0,
    }
    return result


def build(provider: str, model: str, temperature: float, max_tokens: int) -> dict:
    system_prompt, user_prompt = split_prompt(
        (PROMPTS / "item_extractor.md").read_text(encoding="utf-8")
    )
    user_prompt = (
        user_prompt.replace("{{department}}", "{{#block_fields.department#}}")
        .replace("{{work_section}}", "{{#block_fields.work_section#}}")
        .replace("{{delivery_group}}", "{{#block_fields.delivery_group#}}")
        .replace("{{raw_text}}", "{{#block_fields.raw_text#}}")
    )

    nodes = []
    edges = []

    # ---- Start ----------------------------------------------------
    nodes.append(
        {
            "data": {
                "desc": "上传一份完整会议 PDF/DOCX",
                "selected": False,
                "title": "开始",
                "type": "start",
                "variables": [
                    {
                        "allowed_file_extensions": [".pdf", ".docx", ".txt"],
                        "allowed_file_types": ["document"],
                        "allowed_file_upload_methods": ["local_file", "remote_url"],
                        "label": "会议文件",
                        "options": [],
                        "required": True,
                        "type": "file",
                        "variable": "meeting_file",
                    },
                    {
                        "label": "Block 字符上限",
                        "max_length": 8,
                        "options": [],
                        "required": False,
                        "type": "text-input",
                        "variable": "max_block_chars",
                    },
                ],
            },
            "height": 142,
            "id": "start",
            "position": {"x": 30, "y": 300},
            "positionAbsolute": {"x": 30, "y": 300},
            "selected": False,
            "sourcePosition": "right",
            "targetPosition": "left",
            "type": "custom",
            "width": 244,
        }
    )

    # ---- Document Extractor ---------------------------------------
    nodes.append(
        {
            "data": {
                "desc": "PDF/DOCX → 纯文本",
                "is_array_file": False,
                "selected": False,
                "title": "文档提取",
                "type": "document-extractor",
                "variable_selector": ["start", "meeting_file"],
            },
            "height": 92,
            "id": "doc_extract",
            "position": {"x": 330, "y": 300},
            "positionAbsolute": {"x": 330, "y": 300},
            "selected": False,
            "sourcePosition": "right",
            "targetPosition": "left",
            "type": "custom",
            "width": 244,
        }
    )
    edges.append(edge("start", "doc_extract", "start", "document-extractor"))

    # ---- Code: 规范化 + 结构切分 -----------------------------------
    nodes.append(
        code_node(
            "prepare",
            "文本规范化+结构切分",
            load_code("n1_prepare.py"),
            {"doc_text": "string", "blocks": "array[object]", "block_count": "number"},
            [
                ("doc_text", ["doc_extract", "text"]),
                ("max_block_chars", ["start", "max_block_chars"]),
            ],
            {"x": 630, "y": 300},
            desc="页码清理、条目行合并、部门/板块/交付组层级还原；start_char 由此确定",
        )
    )
    edges.append(edge("doc_extract", "prepare", "document-extractor", "code"))

    # ---- Iteration ------------------------------------------------
    nodes.append(
        {
            "data": {
                "desc": "每个 Block 独立抽取，互不污染",
                "error_handle_mode": "continue-on-error",
                "height": 250,
                "is_parallel": True,
                "iterator_selector": ["prepare", "blocks"],
                "output_selector": ["collect", "round_json"],
                "output_type": "array[string]",
                "parallel_nums": 3,
                "selected": False,
                "startNodeType": "custom-iteration-start",
                "start_node_id": "iter_start",
                "title": "逐 Block 抽取",
                "type": "iteration",
                "width": 1020,
            },
            "height": 250,
            "id": "iteration",
            "position": {"x": 930, "y": 200},
            "positionAbsolute": {"x": 930, "y": 200},
            "selected": False,
            "sourcePosition": "right",
            "targetPosition": "left",
            "type": "custom-iteration",
            "width": 1020,
            "zIndex": 1,
        }
    )
    edges.append(edge("prepare", "iteration", "code", "iteration"))

    nodes.append(
        {
            "data": {
                "desc": "",
                "isInIteration": True,
                "selected": False,
                "title": "",
                "type": "iteration-start",
            },
            "draggable": False,
            "height": 48,
            "id": "iter_start",
            "parentId": "iteration",
            "position": {"x": 24, "y": 100},
            "positionAbsolute": {"x": 954, "y": 300},
            "selectable": False,
            "sourcePosition": "right",
            "targetPosition": "left",
            "type": "custom-iteration-start",
            "width": 44,
            "zIndex": 1002,
        }
    )

    nodes.append(
        code_node(
            "block_fields",
            "展开 Block 字段",
            load_code("n2_block_fields.py"),
            {
                "department": "string",
                "work_section": "string",
                "delivery_group": "string",
                "raw_text": "string",
                "block_index": "number",
            },
            [("block", ["iteration", "item"])],
            {"x": 110, "y": 80},
            parent="iteration",
        )
    )
    edges.append(
        edge("iter_start", "block_fields", "iteration-start", "code", True, "iteration")
    )

    nodes.append(
        {
            "data": {
                "context": {"enabled": False, "variable_selector": []},
                "desc": "只产出 evidence_text，不猜字符位置",
                "isInIteration": True,
                "iteration_id": "iteration",
                "model": {
                    "completion_params": {
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    "mode": "chat",
                    "name": model,
                    "provider": provider,
                },
                "prompt_template": [
                    {"id": "sys", "role": "system", "text": system_prompt},
                    {"id": "usr", "role": "user", "text": user_prompt},
                ],
                "selected": False,
                "title": "Item Extractor",
                "type": "llm",
                "variables": [],
                "vision": {"enabled": False},
            },
            "extent": "parent",
            "height": 90,
            "id": "llm_extract",
            "parentId": "iteration",
            "position": {"x": 400, "y": 80},
            "positionAbsolute": {"x": 1330, "y": 280},
            "selected": False,
            "sourcePosition": "right",
            "targetPosition": "left",
            "type": "custom",
            "width": 244,
            "zIndex": 1001,
        }
    )
    edges.append(edge("block_fields", "llm_extract", "code", "llm", True, "iteration"))

    nodes.append(
        code_node(
            "collect",
            "解析本轮输出",
            load_code("n3_collect.py"),
            {"round_json": "string"},
            [
                ("llm_text", ["llm_extract", "text"]),
                ("block_index", ["block_fields", "block_index"]),
            ],
            {"x": 700, "y": 80},
            parent="iteration",
        )
    )
    edges.append(edge("llm_extract", "collect", "llm", "code", True, "iteration"))

    # ---- Flatten / Align / Quality / Validate ----------------------
    nodes.append(
        code_node(
            "flatten",
            "Flatten",
            load_code("n4_flatten.py"),
            {
                "candidates_json": "string",
                "dropped_json": "string",
                "normalize_warnings_json": "string",
                "block_errors_json": "string",
                "candidate_count": "number",
            },
            [("rounds", ["iteration", "output"])],
            {"x": 2010, "y": 300},
        )
    )
    edges.append(edge("iteration", "flatten", "iteration", "code"))

    nodes.append(
        code_node(
            "align",
            "Evidence Aligner",
            load_code("n5_align.py"),
            {"items_json": "string", "exact_match_count": "number", "item_count": "number"},
            [
                ("candidates_json", ["flatten", "candidates_json"]),
                ("doc_text", ["prepare", "doc_text"]),
                ("blocks", ["prepare", "blocks"]),
            ],
            {"x": 2310, "y": 300},
            desc="start_char/end_char/exact_match 全部由代码算出",
        )
    )
    edges.append(edge("flatten", "align", "code", "code"))

    nodes.append(
        code_node(
            "quality",
            "基础质量门",
            load_code("n6_quality_gate.py"),
            {
                "issues_json": "string",
                "error_count": "number",
                "warning_count": "number",
                "has_error": "string",
            },
            [
                ("items_json", ["align", "items_json"]),
                ("doc_text", ["prepare", "doc_text"]),
            ],
            {"x": 2610, "y": 300},
            desc="不静默修正，问题写入 issues_json",
        )
    )
    edges.append(edge("align", "quality", "code", "code"))

    nodes.append(
        code_node(
            "validator",
            "Final JSON Schema Validator",
            load_code("n7_schema_validator.py"),
            {"items": "array[object]", "item_count": "number"},
            [("items_json", ["align", "items_json"])],
            {"x": 2910, "y": 300},
            desc="不合规直接抛错，绝不输出看起来正常的结果",
        )
    )
    edges.append(edge("quality", "validator", "code", "code"))

    nodes.append(
        {
            "data": {
                "desc": "只输出 items",
                "outputs": [{"value_selector": ["validator", "items"], "variable": "items"}],
                "selected": False,
                "title": "结束",
                "type": "end",
            },
            "height": 90,
            "id": "end",
            "position": {"x": 3210, "y": 300},
            "positionAbsolute": {"x": 3210, "y": 300},
            "selected": False,
            "sourcePosition": "right",
            "targetPosition": "left",
            "type": "custom",
            "width": 244,
        }
    )
    edges.append(edge("validator", "end", "code", "end"))

    return {
        "app": {
            "description": (
                "M1 Meeting Item Extraction：从一份完整会议 PDF/DOCX 高召回、忠实地"
                "抽取结构化业务事项。不访问历史任务、不判断 CREATE/UPDATE、"
                "不做跨会议匹配、不做项目别名归一。"
            ),
            "icon": "🗂️",
            "icon_background": "#FFEAD5",
            "mode": "workflow",
            "name": "M1 Meeting Item Extraction",
            "use_icon_as_answer_icon": False,
        },
        "kind": "app",
        "version": "0.1.5",
        "workflow": {
            "conversation_variables": [],
            "environment_variables": [],
            "features": {
                "file_upload": {
                    "allowed_file_extensions": [".pdf", ".docx", ".txt"],
                    "allowed_file_types": ["document"],
                    "allowed_file_upload_methods": ["local_file", "remote_url"],
                    "enabled": False,
                    "fileUploadConfig": {
                        "file_size_limit": 15,
                        "workflow_file_upload_limit": 10,
                    },
                    "image": {"enabled": False, "number_limits": 3, "transfer_methods": []},
                    "number_limits": 3,
                },
                "opening_statement": "",
                "retriever_resource": {"enabled": True},
                "sensitive_word_avoidance": {"enabled": False},
                "speech_to_text": {"enabled": False},
                "suggested_questions": [],
                "suggested_questions_after_answer": {"enabled": False},
                "text_to_speech": {"enabled": False, "language": "", "voice": ""},
            },
            "graph": {
                "edges": edges,
                "nodes": nodes,
                "viewport": {"x": 0, "y": 0, "zoom": 0.6},
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 M1 Dify Workflow DSL")
    # 默认指向服务器本地 Qwen3.5（qwen35_9b_api 暴露 OpenAI 兼容接口）。
    # 换成 Ollama 时：--provider langgenius/ollama/ollama --model qwen3:30b
    parser.add_argument(
        "--provider",
        default="langgenius/openai_api_compatible/openai_api_compatible",
    )
    parser.add_argument("--model", default="Qwen3.5-9B")
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("-o", "--output", default=str(Path(__file__).parent / "m1_workflow.yml"))
    args = parser.parse_args()

    dsl = build(args.provider, args.model, args.temperature, args.max_tokens)
    Path(args.output).write_text(
        yaml.safe_dump(dsl, allow_unicode=True, sort_keys=False, width=10000),
        encoding="utf-8",
    )
    print("已生成 {}".format(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
