# -*- coding: utf-8 -*-
"""结构切分与全链路测试。

Item Extractor 用假模型替身，保证测试不依赖在线模型；
真实模型的抽取质量由 eval/compare_annotation.py 做回归评测。
"""

import pytest
from conftest import FIXTURES  # noqa: F401 - 同时把 src 注入 sys.path

from document_reader import read_document
from generic_extractor import load_prompt as load_generic_prompt
from generic_extractor import render_prompt as render_generic_prompt
from item_extractor import enforce_non_task_policy, load_prompt, normalize_item, render_prompt
from llm_client import LLMClient, LLMConfig, extract_json
from pipeline import run_pipeline
from quality_gate import check_items
from schema_validator import validate_items
from structure_segmenter import segment_blocks
from text_normalizer import normalize

SAMPLE = FIXTURES / "sample_meeting.txt"


def _doc():
    return normalize(read_document(SAMPLE))


def _blocks():
    return segment_blocks(_doc())


def _find(blocks, needle):
    for block in blocks:
        if needle in block.raw_text:
            return block
    raise AssertionError("未找到包含 {!r} 的 Block".format(needle))


# ---- 结构切分 -------------------------------------------------------


def test_context_inheritance():
    block = _find(_blocks(), "红沙泉项目")
    assert block.department == "智能矿山事业部"
    assert block.work_section == "项目推进"
    assert block.delivery_group == "新疆交付组"


def test_block_offsets_map_back_to_source():
    doc = _doc()
    for block in segment_blocks(doc):
        assert doc.text[block.start_char : block.end_char] == block.raw_text


def test_project_actions_stay_in_one_block():
    """Case 3：项目下多动作不能被切断。"""
    block = _find(_blocks(), "①数据中心建设")
    assert "完成吊顶施工" in block.raw_text
    assert "②综合管控平台研发" in block.raw_text


def test_multi_project_paragraph_kept_in_one_block():
    """Case 2：西安研究院三个项目在同一个 Block，由 Item Extractor 再拆。"""
    block = _find(_blocks(), "CRM二期项目")
    assert "数据中台项目" in block.raw_text
    assert "煤矿复合灾害监测预警系统项目" in block.raw_text


def test_similar_project_names_are_separate_blocks_or_lines():
    """Case 7：红沙泉项目 与 红沙泉二矿项目 必须各自可辨认，不能被合并。"""
    doc = _doc()
    assert "红沙泉项目" in doc.text
    assert "红沙泉二矿项目" in doc.text
    blocks = segment_blocks(doc)
    assert _find(blocks, "红沙泉项目") is not None


def test_no_block_splits_mid_line():
    doc = _doc()
    starts = {line.start for line in doc.lines}
    ends = {line.end for line in doc.lines}
    for block in segment_blocks(doc):
        assert block.start_char in starts
        assert block.end_char in ends


def test_max_block_chars_respected_at_safe_boundaries():
    doc = _doc()
    blocks = segment_blocks(doc, max_block_chars=60)
    assert len(blocks) > len(segment_blocks(doc, max_block_chars=100000))
    for block in blocks:
        assert doc.text[block.start_char : block.end_char] == block.raw_text


# ---- 提示词渲染 -----------------------------------------------------


def test_prompt_carries_block_context_and_text():
    block = _find(_blocks(), "红沙泉项目")
    system, user = render_prompt(load_prompt(), block)
    assert "{{raw_text}}" not in user and "{{department}}" not in user
    assert block.raw_text in user
    assert "新疆交付组" in user
    assert "PROJECT_TASK" in system
    assert "招标、投标、采购或合同流程" in system
    assert "保持沟通" in system
    assert "content 不得" in system
    # 位置字段必须留给程序算
    assert "start_char" in system and "由程序计算" in system


def test_generic_prompt_carries_whole_document_text():
    doc = _doc()
    system, user = render_generic_prompt(load_generic_prompt(), doc)
    assert "{{meeting_text}}" not in user
    assert doc.text in user
    assert user.endswith(">>>")
    assert "GENERAL_WORK" in system
    assert "兜底召回" in system
    assert "招标、投标、采购和合同流程" in user
    assert "验收后的例行运维" in user


@pytest.mark.parametrize(
    "text, expected",
    [
        ("编制知识产权代理项目招标文件", "NON_TASK_ITEM"),
        ("验收后运维", "NON_TASK_ITEM"),
        ("日常沟通，保持联系", "NON_TASK_ITEM"),
        ("售前项目跟进", "NON_TASK_ITEM"),
        ("保持日常沟通", "NON_TASK_ITEM"),
        ("合同审查系统接口开发并部署", "PROJECT_TASK"),
        ("处理CRM运维故障并完成联调", "PROJECT_TASK"),
        ("完成合同签订并开展项目审计、验收和专利交底", "PROJECT_TASK"),
        ("确定财务升级方案并启动采购", "PROJECT_TASK"),
    ],
)
def test_non_task_business_boundary(text, expected):
    assert enforce_non_task_policy("PROJECT_TASK", text, text)[0] == expected


# ---- 假模型全链路 ---------------------------------------------------


class FakeLLM(LLMClient):
    """按 Block 内容返回预置 items，覆盖需求第十八节的 7 个用例。"""

    def __init__(self, responses):  # 不调用父类 __init__，避免依赖环境变量
        self.responses = responses
        self.config = LLMConfig(
            base_url="fake",
            api_key="EMPTY",
            model="fake-model",
            timeout=1,
            transport="fake",
            num_ctx=None,
            temperature=0,
            max_tokens=1024,
            enable_thinking=False,
            extra_body={},
        )
        self.calls = 0
        self.repairs = 0
        self.failures = 0
        self.truncations = 0
        self.reasoning_fallbacks = 0

    def complete_json(self, system, user, retries=2):
        self.calls += 1
        for needle, payload in self.responses:
            if needle in user:
                return payload
        return {"items": []}


def _item(**kwargs):
    base = {
        "department": None,
        "work_section": None,
        "delivery_group": None,
        "project": None,
        "item_type": "NON_PROJECT_WORK",
        "assignee": [],
        "title": "标题",
        "content": "内容",
        "evidence_text": "内容",
    }
    base.update(kwargs)
    return base


CASES = [
    # 经营工作 Block：Case 6（非项目工作）与 Case 1（普通项目）同处一个 Block
    (
        "参加西安展会",
        {
            "items": [
                _item(
                    item_type="NON_PROJECT_WORK",
                    title="参加西安展会并接待客户",
                    content="参加西安展会，接待相关客户。",
                    evidence_text="（1）参加西安展会，接待相关客户。",
                ),
                _item(
                    project="塔然高勒项目",
                    item_type="PROJECT_TASK",
                    title="入井培训与到货协调",
                    content="组织开展入井培训，协调和组织到货相关事宜。",
                    evidence_text="（2）塔然高勒项目组织开展入井培训，协调和组织到货相关事宜。",
                ),
            ]
        },
    ),
    (
        "①数据中心建设",
        {
            "items": [
                _item(
                    project="红沙泉项目",
                    item_type="PROJECT_TASK",
                    title="数据中心建设",
                    content="完成顶面线管安装；完成吊顶施工；空调设备到货。",
                    evidence_text="①数据中心建设：完成顶面线管安装；完成吊顶施工；空调设备到货。",
                ),
                _item(
                    project="红沙泉项目",
                    item_type="PROJECT_TASK",
                    title="综合管控平台研发",
                    content="完成流程设计功能研发。",
                    evidence_text="②综合管控平台研发：完成流程设计功能研发。",
                ),
                _item(
                    project="红沙泉二矿项目",
                    item_type="PROJECT_TASK",
                    title="现场实施推进",
                    content="推进现场实施，完成机房重建监督。",
                    evidence_text="2）红沙泉二矿项目：推进现场实施，完成机房重建监督。",
                ),
                _item(
                    project="武家塔项目",
                    item_type="NON_TASK_ITEM",
                    title="暂无工作安排",
                    content="暂无。",
                    evidence_text="3）武家塔项目：暂无。",
                ),
            ]
        },
    ),
    (
        "煤矿复合灾害机理课题",
        {
            "items": [
                _item(
                    item_type="RESEARCH_TASK",
                    title="煤矿复合灾害机理课题研究",
                    content="开展煤矿复合灾害机理课题研究，完成阶段性技术报告。",
                    evidence_text="（1）开展煤矿复合灾害机理课题研究，完成阶段性技术报告。",
                )
            ]
        },
    ),
    (
        "参加西安展会",
        {
            "items": [
                _item(
                    item_type="NON_PROJECT_WORK",
                    title="参加西安展会并接待客户",
                    content="参加西安展会，接待相关客户。",
                    evidence_text="（1）参加西安展会，接待相关客户。",
                )
            ]
        },
    ),
    (
        "CRM二期项目",
        {
            "items": [
                _item(
                    project="CRM二期项目",
                    item_type="PROJECT_TASK",
                    assignee=["尤梦雅"],
                    title="运维问题处理与二期方案汇报",
                    content="运维问题处理；二期方案汇报。",
                    evidence_text="一是CRM二期项目（尤梦雅）：运维问题处理；二期方案汇报。",
                ),
                _item(
                    project="数据中台项目",
                    item_type="PROJECT_TASK",
                    assignee=["易超"],
                    title="持续优化与用户问题处理",
                    content="持续优化，解决用户相关问题。",
                    evidence_text="二是数据中台项目（易超）：持续优化，解决用户相关问题。",
                ),
                _item(
                    project="煤矿复合灾害监测预警系统项目",
                    item_type="PROJECT_TASK",
                    assignee=["王超"],
                    title="模型测试与数据同步",
                    content="模型测试；数据同步问题处理。",
                    evidence_text="三是煤矿复合灾害监测预警系统项目（王超）：模型测试；数据同步问题处理。",
                ),
            ]
        },
    ),
]


def _run():
    return run_pipeline(SAMPLE, client=FakeLLM(CASES))


class FakeGenericLLM(FakeLLM):
    def __init__(self, payload):
        super().__init__([])
        self.payload = payload
        self.last_user = None

    def complete_json(self, system, user, retries=2):
        self.calls += 1
        self.last_user = user
        return self.payload


GENERIC_TASKS = {
    "tasks": [
        {
            "department": "智能矿山事业部",
            "project_group": None,
            "project": None,
            "task_category": "GENERAL_WORK",
            "title": "参加西安展会并接待客户",
            "description": "参加西安展会，接待相关客户。",
            "assignee": [],
            "source_segment": "（1）参加西安展会，接待相关客户。",
        },
        {
            "department": "智能矿山事业部",
            "project_group": None,
            "project": "塔然高勒项目",
            "task_category": "PROJECT_TASK",
            "title": "塔然高勒项目入井培训及到货协调",
            "description": "组织开展入井培训，协调和组织到货相关事宜。",
            "assignee": [],
            "source_segment": "（2）塔然高勒项目组织开展入井培训，协调和组织到货相关事宜。",
        },
        {
            "department": "智能矿山事业部",
            "project_group": "新疆交付组",
            "project": "武家塔项目",
            "task_category": "NON_TASK_ITEM",
            "title": "武家塔项目暂无安排",
            "description": "暂无。",
            "assignee": [],
            "source_segment": "3）武家塔项目：暂无。",
        },
    ]
}


def _run_generic(payload=GENERIC_TASKS):
    return run_pipeline(SAMPLE, client=FakeGenericLLM(payload), mode="generic")


def test_pipeline_output_matches_schema():
    result = _run()
    assert validate_items(result.items_payload(), strict=False) == []
    assert set(result.items_payload()) == {"items"}


def test_generic_mode_maps_tasks_to_m1_schema():
    result = _run_generic()
    assert result.mode == "generic"
    assert result.report()["mode"] == "generic"
    assert len(result.blocks) == 1
    assert result.raw_task_count == 3
    assert validate_items(result.items_payload(), strict=False) == []
    assert [i["item_type"] for i in result.items] == [
        "NON_PROJECT_WORK",
        "PROJECT_TASK",
        "NON_TASK_ITEM",
    ]
    assert result.items[0]["work_section"] is None
    assert result.items[2]["delivery_group"] == "新疆交付组"
    assert all(i["evidence"]["exact_match"] for i in result.items)


def test_generic_mode_uses_whole_document_once():
    client = FakeGenericLLM(GENERIC_TASKS)
    result = run_pipeline(SAMPLE, client=client, mode="generic")
    assert client.calls == 1
    assert "CRM二期项目" in client.last_user
    assert len(result.items) == 3


def test_case1_simple_project_task():
    items = _run().items
    match = [i for i in items if i["project"] == "塔然高勒项目"]
    assert len(match) == 1
    assert match[0]["item_type"] == "PROJECT_TASK"
    assert match[0]["evidence"]["exact_match"] is True


def test_case2_three_projects_become_three_items():
    items = _run().items
    projects = {i["project"] for i in items}
    assert {"CRM二期项目", "数据中台项目", "煤矿复合灾害监测预警系统项目"} <= projects
    spans = [
        (i["evidence"]["start_char"], i["evidence"]["end_char"])
        for i in items
        if i["project"] in {"CRM二期项目", "数据中台项目"}
    ]
    assert len(set(spans)) == 2, "不同 Item 必须有各自的 evidence 范围"


def test_case3_multi_action_single_item_keeps_content():
    items = _run().items
    match = [i for i in items if i["title"] == "数据中心建设"]
    assert len(match) == 1
    for action in ("顶面线管安装", "吊顶施工", "空调设备到货"):
        assert action in match[0]["content"]


def test_case4_zanwu_is_non_task_item_with_project():
    items = _run().items
    match = [i for i in items if i["project"] == "武家塔项目"]
    assert len(match) == 1
    assert match[0]["item_type"] == "NON_TASK_ITEM"
    assert match[0]["content"] == "暂无。"


def test_case5_research_task_without_project():
    items = _run().items
    match = [i for i in items if i["item_type"] == "RESEARCH_TASK"]
    assert len(match) == 1
    assert match[0]["project"] is None


def test_case6_non_project_work():
    items = _run().items
    match = [i for i in items if "西安展会" in i["content"]]
    assert len(match) == 1
    assert match[0]["item_type"] == "NON_PROJECT_WORK"
    assert match[0]["project"] is None


def test_case7_similar_project_names_not_normalized():
    items = _run().items
    projects = {i["project"] for i in items}
    assert "红沙泉项目" in projects
    assert "红沙泉二矿项目" in projects


def test_assignee_extracted_as_array():
    items = _run().items
    crm = [i for i in items if i["project"] == "CRM二期项目"][0]
    assert crm["assignee"] == ["尤梦雅"]
    for item in items:
        assert isinstance(item["assignee"], list)


def test_context_fields_inherited_into_items():
    items = _run().items
    crm = [i for i in items if i["project"] == "CRM二期项目"][0]
    assert crm["department"] == "数字化技术服务事业部"


def test_quality_gate_clean_on_good_output():
    result = _run()
    errors = [i for i in check_items(result.items_payload(), result.document) if i.severity == "error"]
    assert errors == [], [e.to_dict() for e in errors]


def test_all_evidence_exact_match():
    result = _run()
    assert all(i["evidence"]["exact_match"] for i in result.items)


def test_report_carries_diagnostics_without_polluting_items():
    result = _run()
    report = result.report()
    assert result.schema_errors == []
    for key in ("schema_errors", "issues", "dropped_candidates", "block_errors", "llm"):
        assert key in report
    # 业务输出里不能出现任何诊断字段
    assert set(result.items_payload()) == {"items"}


# ---- 结构归一的拒绝路径 ---------------------------------------------


def test_illegal_item_type_is_dropped_not_silently_fixed():
    doc = _doc()
    block = _find(segment_blocks(doc), "塔然高勒项目")
    item, reason, _ = normalize_item(_item(item_type="CREATE"), block, doc)
    assert item is None
    assert "item_type" in reason


def test_assignee_null_is_coerced_to_empty_list():
    doc = _doc()
    block = _find(segment_blocks(doc), "塔然高勒项目")
    item, _, warnings = normalize_item(_item(assignee=None), block, doc)
    assert item["assignee"] == []
    assert warnings == []


def test_org_name_as_assignee_clears_field_but_keeps_item():
    """单个附属字段不对，不该赔上整条业务事项；但必须留下告警。"""
    doc = _doc()
    block = _find(segment_blocks(doc), "塔然高勒项目")
    item, reason, warnings = normalize_item(
        _item(assignee=["数字化技术服务事业部"]), block, doc
    )
    assert reason is None
    assert item is not None and item["assignee"] == []
    assert warnings and "assignee" in warnings[0]


def test_composed_project_name_is_rejected():
    """把上级单位名和项目名拼起来，原文里不存在，必须置空而不是编造。"""
    doc = _doc()
    block = _find(segment_blocks(doc), "CRM二期项目")
    item, reason, warnings = normalize_item(
        _item(project="西安研究院CRM二期项目"), block, doc
    )
    assert reason is None
    assert item["project"] is None
    assert warnings and "非原文连续片段" in warnings[0]


def test_verbatim_project_name_is_kept():
    doc = _doc()
    block = _find(segment_blocks(doc), "CRM二期项目")
    item, _, warnings = normalize_item(_item(project="CRM二期项目"), block, doc)
    assert item["project"] == "CRM二期项目"
    assert warnings == []


def test_project_with_source_whitespace_is_kept():
    """原文 'ERP 项目' 带空格，忽略空白后仍算命中。"""
    doc = _doc()
    block = _find(segment_blocks(doc), "红沙泉项目")
    item, _, warnings = normalize_item(_item(project="红沙泉 项目"), block, doc)
    assert item["project"] == "红沙泉 项目"
    assert warnings == []


def test_project_from_other_block_is_kept_but_flagged():
    doc = _doc()
    block = _find(segment_blocks(doc), "参加西安展会")
    item, _, warnings = normalize_item(_item(project="红沙泉项目"), block, doc)
    assert item["project"] == "红沙泉项目"
    assert warnings and "不在本 Block 内" in warnings[0]


def test_normalize_warnings_reach_the_report():
    class OrgAssigneeLLM(FakeLLM):
        def complete_json(self, system, user, retries=2):
            self.calls += 1
            return {"items": [_item(assignee=["数字化技术服务事业部"])]}

    result = run_pipeline(SAMPLE, client=OrgAssigneeLLM([]))
    assert result.items, "不应该因为 assignee 丢掉整条 Item"
    assert result.normalize_warnings
    assert "normalize_warnings" in result.report()


def test_dropped_candidates_are_reported():
    class BadLLM(FakeLLM):
        def complete_json(self, system, user, retries=2):
            self.calls += 1
            return {"items": [_item(item_type="UPDATE")]}

    result = run_pipeline(SAMPLE, client=BadLLM([]))
    assert result.items == []
    assert result.dropped, "非法候选必须进入 dropped 而不是被悄悄修正"


def test_llm_json_failure_is_recorded_as_block_error():
    class NullLLM(FakeLLM):
        def complete_json(self, system, user, retries=2):
            self.calls += 1
            return None

    result = run_pipeline(SAMPLE, client=NullLLM([]))
    assert result.items == []
    assert result.block_errors


def test_generic_invalid_task_category_is_dropped():
    payload = {
        "tasks": [
            {
                "department": "智能矿山事业部",
                "project_group": None,
                "project": None,
                "task_category": "CREATE",
                "title": "非法类型",
                "description": "参加西安展会，接待相关客户。",
                "assignee": [],
                "source_segment": "（1）参加西安展会，接待相关客户。",
            }
        ]
    }
    result = _run_generic(payload)
    assert result.items == []
    assert result.dropped
    assert "task_category" in result.dropped[0]["reason"]


# ---- JSON 解析健壮性 -------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"items":[]}',
        '```json\n{"items":[]}\n```',
        '<think>先想一下</think>{"items":[]}',
        '好的，结果如下：\n{"items":[]}\n',
    ],
)
def test_extract_json_tolerates_wrappers(raw):
    assert extract_json(raw) == {"items": []}


def test_extract_json_returns_none_on_garbage():
    assert extract_json("完全不是 JSON") is None
