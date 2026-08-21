"""合成 M2 输出工厂：构造严格符合 schemas/m2_output.schema.json 的测试数据。

仅用于单元测试；字段与 M2 官方契约一一对应（additionalProperties=false）。
"""
from __future__ import annotations


def evidence(text: str, page_start: int = 1, page_end: int = 1,
             start_char: int = 0, end_char: int | None = None,
             exact_match: bool = True) -> dict:
    return {
        "text": text,
        "page_start": page_start,
        "page_end": page_end,
        "start_char": start_char,
        "end_char": len(text) if end_char is None else end_char,
        "exact_match": exact_match,
    }


def item(department: str | None = None, work_section: str | None = None,
         delivery_group: str | None = None, project: str | None = None,
         item_type: str = "PROJECT_TASK", assignee: list[str] | None = None,
         title: str = "标题", content: str = "内容", ev: dict | None = None) -> dict:
    return {
        "department": department,
        "work_section": work_section,
        "delivery_group": delivery_group,
        "project": project,
        "item_type": item_type,
        "assignee": assignee if assignee is not None else [],
        "title": title,
        "content": content,
        "evidence": ev or evidence("默认证据文本"),
    }


def project_entity(entity_id: str, canonical_name: str, aliases: list[str] | None = None,
                   source_names: list[str] | None = None, decisions: list[dict] | None = None) -> dict:
    return {
        "entity_id": entity_id,
        "canonical_name": canonical_name,
        "aliases": aliases if aliases is not None else [],
        "source_names": source_names if source_names is not None else [],
        "decisions": decisions if decisions is not None else [],
    }


def decision(decision: str = "SAME_ENTITY", type: str = "intra_meeting",
             left: str | None = None, right: str | None = None) -> dict:
    return {"type": type, "left": left, "right": right, "decision": decision}


def trace(item_index: int, source_indexes: list[int], merged: bool = False,
          evidence_mode: str = "single", evidence_contiguous: bool = True,
          source_evidence: list[dict] | None = None, project_entity_id: str | None = None,
          title_source: str = "source", notes: list[str] | None = None) -> dict:
    return {
        "item_index": item_index,
        "source_indexes": source_indexes,
        "merged": merged,
        "evidence_mode": evidence_mode,
        "evidence_contiguous": evidence_contiguous,
        "source_evidence": source_evidence if source_evidence is not None else [],
        "project_entity_id": project_entity_id,
        "title_source": title_source,
        "notes": notes if notes is not None else [],
    }


def m2_output(source_mode: str = "generic", items: list[dict] | None = None,
              project_entities: list[dict] | None = None, merge_trace: list[dict] | None = None,
              status: str = "PASS") -> dict:
    return {
        "source_mode": source_mode,
        "items": items if items is not None else [],
        "project_entities": project_entities if project_entities is not None else [],
        "merge_trace": merge_trace if merge_trace is not None else [],
        "validation": {"status": status, "issues": []},
    }


def rich_pass_m2(source_mode: str = "generic") -> dict:
    """一份信息量充分的 PASS 输出：含 alias、合并 provenance、人员复用、
    work_section/delivery_group/department/project 为 null 的边界 item。"""
    ev0 = evidence("①红沙泉二矿：完成集控中心施工。\n②推进数据中心落地，至100%。",
                   page_start=2, page_end=2, start_char=442, end_char=538)
    ev0b = evidence("跟进红沙泉二矿项目施工进度。", page_start=2, page_end=2, start_char=539, end_char=560)
    ev1 = evidence("大柳塔项目推进会议召开。", page_start=1, page_end=1, start_char=10, end_char=30)
    ev2 = evidence("其他非项目事项说明。", page_start=1, page_end=1, start_char=40, end_char=55)

    items = [
        item(department="智能矿山事业部", work_section="矿山板块", delivery_group="榆林交付组",
             project="红沙泉二矿项目", item_type="PROJECT_TASK", assignee=["张三", "李四"],
             title="红沙泉二矿项目集控中心及数据中心施工进度推进",
             content="推进红沙泉二矿项目集控中心与数据中心建设。", ev=ev0),
        item(department="智能矿山事业部", work_section=None, delivery_group=None,
             project="大柳塔项目", item_type="PROJECT_TASK", assignee=["张三"],
             title="大柳塔项目推进", content="推进大柳塔项目。", ev=ev1),
        item(department=None, work_section=None, delivery_group=None, project=None,
             item_type="NON_TASK_ITEM", assignee=[], title="其他事项", content="其他。", ev=ev2),
    ]
    project_entities = [
        project_entity("P0001", "红沙泉二矿智能化建设项目",
                       aliases=["红沙泉二矿项目"],
                       source_names=["红沙泉二矿项目", "红沙泉二矿智能化建设项目"],
                       decisions=[decision("SAME_ENTITY", left="红沙泉二矿项目",
                                           right="红沙泉二矿智能化建设项目")]),
        project_entity("P0002", "大柳塔项目", aliases=[], source_names=["大柳塔项目"]),
    ]
    merge_trace = [
        trace(0, [0, 1], merged=True, evidence_mode="bounding_span", evidence_contiguous=True,
              source_evidence=[ev0, ev0b], project_entity_id="P0001", title_source="llm"),
        trace(1, [2], merged=False, evidence_mode="single", source_evidence=[ev1],
              project_entity_id="P0002", title_source="source"),
        trace(2, [3], merged=False, evidence_mode="single", source_evidence=[ev2],
              project_entity_id=None, title_source="source"),
    ]
    return m2_output(source_mode=source_mode, items=items,
                     project_entities=project_entities, merge_trace=merge_trace, status="PASS")


def second_meeting_m2() -> dict:
    """第二次会议：同一 canonical（红沙泉二矿智能化建设项目）但不同 M2 entity_id（P0009）。"""
    ev = evidence("红沙泉二矿项目继续推进。", page_start=1, page_end=1, start_char=0, end_char=20)
    items = [
        item(department="智能矿山事业部", project="红沙泉二矿智能化建设项目",
             item_type="PROJECT_TASK", assignee=["王五"], title="红沙泉二矿续建", content="续建。", ev=ev),
    ]
    project_entities = [
        project_entity("P0009", "红沙泉二矿智能化建设项目",
                       aliases=["红沙泉二矿项目"], source_names=["红沙泉二矿项目"]),
    ]
    merge_trace = [
        trace(0, [0], merged=False, evidence_mode="single", source_evidence=[ev],
              project_entity_id="P0009", title_source="source"),
    ]
    return m2_output("generic", items, project_entities, merge_trace, "PASS")
