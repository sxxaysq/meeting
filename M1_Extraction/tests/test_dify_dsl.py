# -*- coding: utf-8 -*-
"""Dify DSL 接线校验。

没有 Dify 实例也能提前发现"变量引用了不存在的节点/输出"这类导入后才炸的问题。
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from conftest import FIXTURES  # noqa: F401 - 同时把 src 注入 sys.path

ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    path = ROOT / "dify" / "build_workflow.py"
    spec = importlib.util.spec_from_file_location("dify_build_workflow", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()
DSL = builder.build("langgenius/ollama/ollama", "qwen3:30b", 0, 4096)
GRAPH = DSL["workflow"]["graph"]
NODES = {n["id"]: n for n in GRAPH["nodes"]}
EDGES = GRAPH["edges"]


def _outputs_of(node_id):
    """节点对外可引用的变量名集合。"""
    node = NODES[node_id]
    data = node["data"]
    kind = data["type"]
    if kind == "code":
        return set(data["outputs"])
    if kind == "start":
        return {v["variable"] for v in data["variables"]} | {"sys.files"}
    if kind == "document-extractor":
        return {"text"}
    if kind == "llm":
        return {"text", "usage"}
    if kind == "iteration":
        return {"output", "item", "index"}
    return set()


def test_app_is_workflow_mode():
    assert DSL["app"]["mode"] == "workflow"
    assert DSL["kind"] == "app"


def test_every_edge_connects_existing_nodes():
    for edge in EDGES:
        assert edge["source"] in NODES, edge
        assert edge["target"] in NODES, edge


def test_graph_is_connected_start_to_end():
    outgoing = {}
    for edge in EDGES:
        outgoing.setdefault(edge["source"], []).append(edge["target"])
    seen = set()
    stack = ["start"]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(outgoing.get(node, []))
    assert "end" in seen, "Start 到 End 不连通"


def test_all_variable_selectors_resolve():
    """只有 Code 节点的 variables 是入参引用；Start 的 variables 是表单定义。"""
    for node_id, node in NODES.items():
        if node["data"]["type"] != "code":
            continue
        for variable in node["data"].get("variables", []):
            selector = variable["value_selector"]
            source, name = selector[0], selector[-1]
            assert source in NODES, "{} 引用了不存在的节点 {}".format(node_id, source)
            available = _outputs_of(source)
            assert name in available, "{}.{} 不存在（可用：{}）".format(
                source, name, sorted(available)
            )


def test_iteration_selectors_resolve():
    iteration = NODES["iteration"]["data"]
    source, name = iteration["iterator_selector"]
    assert name in _outputs_of(source)
    source, name = iteration["output_selector"]
    assert name in _outputs_of(source)
    assert iteration["start_node_id"] in NODES


def test_iteration_children_declare_parent():
    for node_id in ("block_fields", "llm_extract", "collect", "iter_start"):
        node = NODES[node_id]
        assert node["parentId"] == "iteration", node_id
        assert node["data"].get("isInIteration") is True, node_id


def test_nodes_outside_iteration_have_no_parent():
    for node_id in ("start", "doc_extract", "prepare", "flatten", "align", "validator", "end"):
        assert "parentId" not in NODES[node_id], node_id


def test_end_outputs_only_items():
    outputs = NODES["end"]["data"]["outputs"]
    assert [o["variable"] for o in outputs] == ["items"]
    source, name = outputs[0]["value_selector"]
    assert (source, name) == ("validator", "items")
    assert NODES["validator"]["data"]["outputs"]["items"]["type"] == "array[object]"


def test_document_extractor_reads_start_file():
    assert NODES["doc_extract"]["data"]["variable_selector"] == ["start", "meeting_file"]


def test_llm_prompt_has_no_unresolved_placeholders():
    prompts = NODES["llm_extract"]["data"]["prompt_template"]
    joined = "\n".join(p["text"] for p in prompts)
    for placeholder in ("{{department}}", "{{work_section}}", "{{delivery_group}}", "{{raw_text}}"):
        assert placeholder not in joined, "未替换的占位符 {}".format(placeholder)
    for reference in (
        "{{#block_fields.department#}}",
        "{{#block_fields.raw_text#}}",
        "{{#block_fields.delivery_group#}}",
    ):
        assert reference in joined, "缺少变量引用 {}".format(reference)


def test_llm_prompt_forbids_position_guessing():
    system = NODES["llm_extract"]["data"]["prompt_template"][0]["text"]
    assert "由程序计算" in system
    for field in ("start_char", "end_char", "exact_match"):
        assert field in system


def test_code_nodes_declare_main():
    for node_id, node in NODES.items():
        if node["data"]["type"] != "code":
            continue
        code = node["data"]["code"]
        assert "def main(" in code, node_id
        for name in node["data"]["variables"]:
            assert name["variable"] in code, "{} 的入参 {} 没在代码里用到".format(
                node_id, name["variable"]
            )


@pytest.mark.parametrize("node_id", ["prepare", "block_fields", "collect", "flatten", "align", "quality", "validator"])
def test_code_nodes_are_python3(node_id):
    assert NODES[node_id]["data"]["code_language"] == "python3"


def test_generated_yaml_roundtrips():
    yaml = pytest.importorskip("yaml")
    text = yaml.safe_dump(DSL, allow_unicode=True, sort_keys=False, width=10000)
    reloaded = yaml.safe_load(text)
    assert reloaded == DSL
