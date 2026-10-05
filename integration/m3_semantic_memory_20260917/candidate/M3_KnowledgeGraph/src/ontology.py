"""M3 固定 Ontology（第一版冻结，禁止 LLM 发明新类型）。

节点：Department / WorkSection / DeliveryGroup / Project / ProjectAlias /
      Person / MeetingItem / SourceDocument
关系：HAS_ITEM / BELONGS_TO / UNDER_SECTION / DELIVERED_BY / ABOUT_PROJECT /
      ASSIGNED_TO / ALIAS_OF / UNDER_DEPARTMENT / IN_SECTION / IN_DEPARTMENT
Task 节点禁止：MeetingItem != Task，Task 生命周期归 M6。
"""
from __future__ import annotations

NODE_LABELS: tuple[str, ...] = (
    "Department",
    "WorkSection",
    "DeliveryGroup",
    "Project",
    "ProjectAlias",
    "Person",
    "MeetingItem",
    "SourceDocument",
)

REL_TYPES: tuple[str, ...] = (
    "HAS_ITEM",          # (SourceDocument)-[:HAS_ITEM]->(MeetingItem)
    "BELONGS_TO",        # (MeetingItem)-[:BELONGS_TO]->(Department)
    "UNDER_SECTION",     # (MeetingItem)-[:UNDER_SECTION]->(WorkSection)
    "DELIVERED_BY",      # (MeetingItem)-[:DELIVERED_BY]->(DeliveryGroup)
    "ABOUT_PROJECT",     # (MeetingItem)-[:ABOUT_PROJECT]->(Project)
    "ASSIGNED_TO",       # (MeetingItem)-[:ASSIGNED_TO]->(Person)
    "ALIAS_OF",          # (ProjectAlias)-[:ALIAS_OF]->(Project)
    "UNDER_DEPARTMENT",  # (WorkSection)-[:UNDER_DEPARTMENT]->(Department)
    "IN_SECTION",        # (DeliveryGroup)-[:IN_SECTION]->(WorkSection)
    "IN_DEPARTMENT",     # (DeliveryGroup)-[:IN_DEPARTMENT]->(Department)
)

# 所有业务节点共有的身份属性（唯一约束建立于此）
ID_PROPERTY = "id"

# 正式入图 vs 实验强制入图的标记值
INGEST_MODE_FORMAL = "formal"      # M2 validation.status == PASS
INGEST_MODE_FORCED = "forced"      # REVIEW/ERROR + --force，全节点打标记
