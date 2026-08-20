# -*- coding: utf-8 -*-
"""Dify Code 节点测试。

YAML 里的 Python 是从 dify/code_nodes/*.py 内联的，所以这里直接测这些模块，
并断言它们与 src/ 主实现产出一致——避免"Dify 里的代码没人测过"。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from conftest import FIXTURES  # noqa: F401 - 同时把 src 注入 sys.path

from document_reader import read_document
from structure_segmenter import segment_blocks
from text_normalizer import normalize

ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = ROOT / "dify" / "code_nodes"
SAMPLE = FIXTURES / "sample_meeting.txt"


def _load(name):
    path = CODE_DIR / name
    spec = importlib.util.spec_from_file_location("dify_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


n1 = _load("n1_prepare.py")
n2 = _load("n2_block_fields.py")
n3 = _load("n3_collect.py")
n4 = _load("n4_flatten.py")
n5 = _load("n5_align.py")
n6 = _load("n6_quality_gate.py")
n7 = _load("n7_schema_validator.py")

RAW = SAMPLE.read_text(encoding="utf-8")


# ---- n1 与 src/ 主实现一致 -------------------------------------------


def test_n1_normalized_text_matches_src():
    doc = normalize(read_document(SAMPLE))
    assert n1.main(RAW, "1200")["doc_text"] == doc.text


def test_n1_blocks_match_src():
    doc = normalize(read_document(SAMPLE))
    expected = segment_blocks(doc)
    actual = n1.main(RAW, "1200")["blocks"]
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected):
        assert got["start_char"] == want.start_char
        assert got["end_char"] == want.end_char
        assert got["department"] == want.department
        assert got["work_section"] == want.work_section
        assert got["delivery_group"] == want.delivery_group
        assert got["raw_text"] == want.raw_text


def test_n1_handles_bad_max_block_chars():
    assert n1.main(RAW, "")["block_count"] > 0
    assert n1.main(RAW, "abc")["block_count"] > 0


# ---- n3 解析 ---------------------------------------------------------


def _llm_payload(**over):
    item = {
        "department": None,
        "work_section": None,
        "delivery_group": None,
        "project": "红沙泉项目",
        "item_type": "PROJECT_TASK",
        "assignee": [],
        "title": "数据中心建设",
        "content": "完成顶面线管安装；完成吊顶施工；空调设备到货。",
        "evidence_text": "①数据中心建设：完成顶面线管安装；完成吊顶施工；空调设备到货。",
    }
    item.update(over)
    return json.dumps({"items": [item]}, ensure_ascii=False)


def test_n3_parses_plain_json():
    out = json.loads(n3.main(_llm_payload(), 3)["round_json"])
    assert len(out["items"]) == 1
    assert out["items"][0]["block_index"] == 3
    assert out["error"] == ""


@pytest.mark.parametrize(
    "wrapper",
    ["```json\n{}\n```", "<think>想一下</think>{}", "结果如下：\n{}"],
)
def test_n3_tolerates_wrappers(wrapper):
    text = wrapper.format(_llm_payload())
    out = json.loads(n3.main(text, 0)["round_json"])
    assert len(out["items"]) == 1


def test_n3_drops_illegal_item_type():
    out = json.loads(n3.main(_llm_payload(item_type="CREATE"), 0)["round_json"])
    assert out["items"] == []
    assert out["dropped"] and "item_type" in out["dropped"][0]["reason"]


def test_n3_records_error_on_garbage():
    out = json.loads(n3.main("完全不是 JSON", 0)["round_json"])
    assert out["error"]


def test_n3_assignee_null_becomes_empty_list():
    out = json.loads(n3.main(_llm_payload(assignee=None), 0)["round_json"])
    assert out["items"][0]["assignee"] == []


# ---- n4 → n5 → n6 → n7 全链路 ----------------------------------------


def _chain(llm_text_by_block=None):
    prepared = n1.main(RAW, "1200")
    blocks = prepared["blocks"]
    rounds = []
    for block in blocks:
        fields = n2.main(block)
        text = (llm_text_by_block or {}).get(block["index"], '{"items":[]}')
        rounds.append(n3.main(text, fields["block_index"])["round_json"])
    flat = n4.main(rounds)
    aligned = n5.main(flat["candidates_json"], prepared["doc_text"], blocks)
    quality = n6.main(aligned["items_json"], prepared["doc_text"])
    return prepared, flat, aligned, quality


def _block_index_containing(needle):
    for block in n1.main(RAW, "1200")["blocks"]:
        if needle in block["raw_text"]:
            return block["index"]
    raise AssertionError("未找到 Block")


def test_full_chain_produces_schema_valid_items():
    index = _block_index_containing("①数据中心建设")
    prepared, flat, aligned, quality = _chain({index: _llm_payload()})
    assert flat["candidate_count"] == 1
    assert aligned["exact_match_count"] == 1
    result = n7.main(aligned["items_json"])
    assert result["item_count"] == 1
    item = result["items"][0]
    assert set(item) == {
        "department",
        "work_section",
        "delivery_group",
        "project",
        "item_type",
        "assignee",
        "title",
        "content",
        "evidence",
    }
    assert item["department"] == "智能矿山事业部"
    assert item["evidence"]["exact_match"] is True
    assert item["evidence"]["page_start"] is None
    assert quality["error_count"] == 0


def test_align_matches_src_implementation():
    """Dify 对齐结果必须与 src/evidence_aligner.py 一致。"""
    from evidence_aligner import align_evidence

    index = _block_index_containing("①数据中心建设")
    prepared, _flat, aligned, _quality = _chain({index: _llm_payload()})
    dify_evidence = json.loads(aligned["items_json"])["items"][0]["evidence"]

    doc = normalize(read_document(SAMPLE))
    block = [b for b in segment_blocks(doc) if b.index == index][0]
    src = align_evidence(doc, block, dify_evidence["text"])
    assert (dify_evidence["start_char"], dify_evidence["end_char"]) == (
        src.start_char,
        src.end_char,
    )
    assert dify_evidence["exact_match"] is src.exact_match


def test_align_does_not_fake_exact_match():
    index = _block_index_containing("①数据中心建设")
    payload = _llm_payload(evidence_text="这段文字原文里根本不存在完全是模型臆造出来的内容")
    _prepared, _flat, aligned, quality = _chain({index: payload})
    evidence = json.loads(aligned["items_json"])["items"][0]["evidence"]
    assert evidence["exact_match"] is False
    codes = {i["code"] for i in json.loads(quality["issues_json"])}
    assert "EVIDENCE_NOT_LOCATED" in codes


def test_quality_gate_flags_invented_project():
    index = _block_index_containing("①数据中心建设")
    payload = _llm_payload(project="根本不存在的某某项目")
    _p, _f, _a, quality = _chain({index: payload})
    codes = {i["code"] for i in json.loads(quality["issues_json"])}
    assert "PROJECT_NOT_IN_SOURCE" in codes
    assert quality["has_error"] == "yes"


def test_quality_gate_flags_invented_assignee():
    index = _block_index_containing("①数据中心建设")
    payload = _llm_payload(assignee=["查无此人"])
    _p, _f, _a, quality = _chain({index: payload})
    codes = {i["code"] for i in json.loads(quality["issues_json"])}
    assert "ASSIGNEE_NOT_IN_SOURCE" in codes


def test_validator_raises_instead_of_returning_plausible_result():
    bad = json.dumps({"items": [{"title": "x"}]}, ensure_ascii=False)
    with pytest.raises(ValueError):
        n7.main(bad)


def test_validator_rejects_forbidden_field():
    doc = normalize(read_document(SAMPLE))
    item = {
        "department": None,
        "work_section": None,
        "delivery_group": None,
        "project": None,
        "item_type": "NON_TASK_ITEM",
        "assignee": [],
        "title": "t",
        "content": "c",
        "confidence": 0.9,
        "evidence": {
            "text": "c",
            "page_start": None,
            "page_end": None,
            "start_char": 0,
            "end_char": 1,
            "exact_match": False,
        },
    }
    with pytest.raises(ValueError) as error:
        n7.main(json.dumps({"items": [item]}, ensure_ascii=False))
    assert "confidence" in str(error.value)


def test_empty_document_yields_empty_items():
    result = n7.main(n5.main("[]", "", [])["items_json"])
    assert result["items"] == []
