# -*- coding: utf-8 -*-
"""Evidence 定位测试：字符位置必须由程序算出，且 exact_match 不得伪造。"""

from conftest import FIXTURES  # noqa: F401 - 同时把 src 注入 sys.path

from document_reader import PageSpan, RawDocument, read_document
from evidence_aligner import align_evidence, build_evidence
from structure_segmenter import segment_blocks
from text_normalizer import normalize

SAMPLE = FIXTURES / "sample_meeting.txt"


def _doc():
    return normalize(read_document(SAMPLE))


def _block_containing(doc, needle):
    for block in segment_blocks(doc):
        if needle in block.raw_text:
            return block
    raise AssertionError("未找到包含 {!r} 的 Block".format(needle))


def test_exact_substring_match():
    doc = _doc()
    block = _block_containing(doc, "完成顶面线管安装")
    evidence = "①数据中心建设：完成顶面线管安装；完成吊顶施工；空调设备到货。"
    result = align_evidence(doc, block, evidence)
    assert result.exact_match is True
    assert result.strategy == "exact"
    assert doc.text[result.start_char : result.end_char] == evidence


def test_reduced_match_ignores_leading_enumeration():
    """模型跨行回抄时去掉了 ①②，仍然算完整定位。"""
    doc = _doc()
    block = _block_containing(doc, "完成顶面线管安装")
    evidence = (
        "数据中心建设：完成顶面线管安装；完成吊顶施工；空调设备到货。\n"
        "综合管控平台研发：完成流程设计功能研发。"
    )
    assert evidence not in doc.text, "该用例必须走不到直接子串匹配"
    result = align_evidence(doc, block, evidence)
    assert result.exact_match is True
    assert result.strategy == "reduced"
    span = doc.text[result.start_char : result.end_char]
    assert span.startswith("①数据中心建设")
    assert span.endswith("完成流程设计功能研发。")


def test_reduced_match_ignores_whitespace():
    doc = _doc()
    block = _block_containing(doc, "完成顶面线管安装")
    evidence = "数据中心建设： 完成顶面线管安装； 完成吊顶施工；空调设备到货。"
    result = align_evidence(doc, block, evidence)
    assert result.exact_match is True


def test_unlocatable_evidence_is_not_faked():
    doc = _doc()
    block = _block_containing(doc, "完成顶面线管安装")
    result = align_evidence(doc, block, "这段文字完全不在原文里出现过属于模型臆造内容")
    assert result.exact_match is False
    assert block.start_char <= result.start_char <= result.end_char <= block.end_char


def test_empty_evidence_is_not_exact():
    doc = _doc()
    block = _block_containing(doc, "完成顶面线管安装")
    assert align_evidence(doc, block, "").exact_match is False


def test_span_stays_inside_block():
    doc = _doc()
    for block in segment_blocks(doc):
        result = align_evidence(doc, block, block.raw_text[:20])
        assert block.start_char <= result.start_char
        assert result.end_char <= block.end_char


def test_build_evidence_has_exactly_six_keys():
    doc = _doc()
    block = _block_containing(doc, "完成顶面线管安装")
    evidence = build_evidence(doc, block, "②综合管控平台研发：完成流程设计功能研发。")
    assert set(evidence) == {
        "text",
        "page_start",
        "page_end",
        "start_char",
        "end_char",
        "exact_match",
    }
    assert evidence["exact_match"] is True


def test_page_numbers_come_from_page_spans():
    raw = RawDocument(
        source_name="x.txt",
        raw_text="第一页内容\n（1）第二页的任务内容。\n",
        page_spans=[PageSpan(1, 0, 6), PageSpan(2, 6, 26)],
    )
    doc = normalize(raw)
    block = segment_blocks(doc)[-1]
    result = align_evidence(doc, block, "第二页的任务内容。")
    assert result.exact_match is True
    assert result.page_start == 2
    assert result.page_end == 2


def test_page_is_none_without_page_spans():
    doc = _doc()  # TXT 没有分页信息
    block = _block_containing(doc, "完成顶面线管安装")
    evidence = build_evidence(doc, block, "②综合管控平台研发：完成流程设计功能研发。")
    assert evidence["page_start"] is None
    assert evidence["page_end"] is None


def test_multiple_items_may_share_one_block_range():
    """同一个 Block 里的多个 Item，各自 evidence 不同、范围可以相邻或重叠。"""
    doc = _doc()
    block = _block_containing(doc, "CRM二期项目")
    first = align_evidence(doc, block, "一是CRM二期项目（尤梦雅）：运维问题处理；二期方案汇报。")
    second = align_evidence(doc, block, "二是数据中台项目（易超）：持续优化，解决用户相关问题。")
    assert first.exact_match and second.exact_match
    assert first.start_char != second.start_char
    for result in (first, second):
        assert block.start_char <= result.start_char < result.end_char <= block.end_char
