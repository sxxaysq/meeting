# -*- coding: utf-8 -*-
"""M2 的数据结构与枚举。

设计要点
--------
* M1 的 9 个业务字段原样保留在 ``items[]`` 里，M2 自己的信息全部放顶层
  （``project_entities`` / ``merge_trace`` / ``validation``），不污染 M1 Schema。
* 所有判定都是三值：确定合、确定不合、拿不准。拿不准一律 REVIEW，
  不允许用浮点 confidence 把"拿不准"糊过去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

ITEM_FIELDS = (
    "department",
    "work_section",
    "delivery_group",
    "project",
    "item_type",
    "assignee",
    "title",
    "content",
    "evidence",
)

ITEM_TYPES = ("PROJECT_TASK", "RESEARCH_TASK", "NON_PROJECT_WORK", "NON_TASK_ITEM")

# item_type 冲突时的取值优先级：越靠前越"像任务"。
# 合并意味着这些 Item 描述同一个业务目标，此时保留信息量最高的那一类，
# 同时必写 ITEM_TYPE_CONFLICT 告警进 REVIEW，不静默吞掉分歧。
ITEM_TYPE_PRIORITY = {name: index for index, name in enumerate(ITEM_TYPES)}

SOURCE_MODES = ("block", "generic")

# ---- 三值判定 ---------------------------------------------------------

SAME_ENTITY = "SAME_ENTITY"
DIFFERENT_ENTITY = "DIFFERENT_ENTITY"
ENTITY_UNCERTAIN = "UNCERTAIN"
ENTITY_DECISIONS = (SAME_ENTITY, DIFFERENT_ENTITY, ENTITY_UNCERTAIN)

MERGE = "MERGE"
KEEP_SEPARATE = "KEEP_SEPARATE"
MERGE_UNCERTAIN = "UNCERTAIN"
MERGE_DECISIONS = (MERGE, KEEP_SEPARATE, MERGE_UNCERTAIN)

KEEP_CLUSTER = "KEEP_CLUSTER"
SPLIT_CLUSTER = "SPLIT_CLUSTER"
CLUSTER_REVIEW = "REVIEW"
CLUSTER_DECISIONS = (KEEP_CLUSTER, SPLIT_CLUSTER, CLUSTER_REVIEW)

PASS = "PASS"
REVIEW = "REVIEW"
ERROR = "ERROR"

# 质量门问题等级
LEVEL_ERROR = "error"
LEVEL_REVIEW = "review"
LEVEL_WARNING = "warning"


@dataclass
class SourceItem:
    """一条 M1 输入 Item，带上它在输入数组里的下标。"""

    index: int
    raw: Dict[str, Any]

    def get(self, name: str, default: Any = None) -> Any:
        return self.raw.get(name, default)

    @property
    def span(self) -> Tuple[int, int]:
        evidence = self.raw.get("evidence") or {}
        return int(evidence.get("start_char") or 0), int(evidence.get("end_char") or 0)

    @property
    def title(self) -> str:
        return str(self.raw.get("title") or "")

    @property
    def content(self) -> str:
        return str(self.raw.get("content") or "")

    @property
    def project(self) -> Optional[str]:
        value = self.raw.get("project")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def assignees(self) -> List[str]:
        value = self.raw.get("assignee")
        return [a for a in value if isinstance(a, str) and a.strip()] if isinstance(value, list) else []


@dataclass
class ProjectEntity:
    """归一后的项目实体。``members`` 是本次会议里指向它的原始写法。"""

    entity_id: str
    canonical_name: str
    aliases: List[str] = field(default_factory=list)
    members: List[str] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "canonical_name": self.canonical_name,
            "aliases": sorted(set(self.aliases)),
            "source_names": sorted(set(self.members)),
            "decisions": self.decisions,
        }


@dataclass
class MergeCandidate:
    """候选对。``signals`` 只解释召回理由，绝不参与最终判定。"""

    left: int
    right: int
    signals: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> Tuple[int, int]:
        return (min(self.left, self.right), max(self.left, self.right))


@dataclass
class PairJudgement:
    left: int
    right: int
    decision: str
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"left": self.left, "right": self.right, "decision": self.decision}


@dataclass
class Cluster:
    """一组准备合并的 source item 下标。size==1 表示原样保留。"""

    members: List[int]
    decision: str = KEEP_CLUSTER
    reason: str = ""
    validated: bool = False

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class Issue:
    code: str
    level: str
    message: str
    item_index: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = {"code": self.code, "level": self.level, "message": self.message}
        if self.item_index is not None:
            payload["item_index"] = self.item_index
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class MergeTraceEntry:
    """一条输出 Item 的来源追溯。合并与未合并都写，保证可逐条回溯。"""

    item_index: int
    source_indexes: List[int]
    merged: bool
    evidence_mode: str
    contiguous: bool
    source_evidence: List[Dict[str, Any]] = field(default_factory=list)
    project_entity_id: Optional[str] = None
    title_source: str = "source"
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_index": self.item_index,
            "source_indexes": self.source_indexes,
            "merged": self.merged,
            "evidence_mode": self.evidence_mode,
            "evidence_contiguous": self.contiguous,
            "source_evidence": self.source_evidence,
            "project_entity_id": self.project_entity_id,
            "title_source": self.title_source,
            "notes": self.notes,
        }
