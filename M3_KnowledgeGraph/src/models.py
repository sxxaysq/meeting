"""M3 内部数据模型（确定性映射的中间表示）。

GraphInput 是 M2 Adapter 的产物：已经完全决定好"写哪些节点/哪些边"，
Neo4jStore 只负责按此计划做幂等 MERGE，不再包含任何业务判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NodePlan:
    label: str
    node_id: str
    props: dict


@dataclass
class EdgePlan:
    rel_type: str
    src_label: str
    src_id: str
    dst_label: str
    dst_id: str


@dataclass
class GraphInput:
    """一次导入（一份 M2 输出）的完整写入计划。"""

    source_document_id: str
    meeting_date: str
    source_mode: str
    validation_status: str
    ingested_at: str
    ingest_mode: str  # formal / forced

    nodes: list[NodePlan] = field(default_factory=list)
    edges: list[EdgePlan] = field(default_factory=list)

    def add_node(self, label: str, node_id: str, props: dict) -> None:
        self.nodes.append(NodePlan(label=label, node_id=node_id, props=props))

    def add_edge(self, rel_type: str, src_label: str, src_id: str, dst_label: str, dst_id: str) -> None:
        self.edges.append(
            EdgePlan(rel_type=rel_type, src_label=src_label, src_id=src_id,
                     dst_label=dst_label, dst_id=dst_id)
        )

    def summary(self) -> dict:
        from collections import Counter

        return {
            "nodes_by_label": dict(Counter(n.label for n in self.nodes)),
            "edges_by_type": dict(Counter(e.rel_type for e in self.edges)),
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
        }


class M2RejectedError(Exception):
    """M2 输出被拒绝入图（非 PASS 且未强制；或结构非法）。"""


class M2AdapterError(Exception):
    """M2 输出结构不符合契约。"""
