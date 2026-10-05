"""Neo4j 存储层：按 GraphInput 计划做幂等写入 + 固定查询。

原则：
- 所有节点 MERGE by id（唯一约束兜底）→ 重复导入不产生重复业务节点；
- 写入只依赖 GraphInput，不做任何业务判断；
- Project 的列表属性（aliases/source_names/m2_entity_ids）跨会议做唯一化合并。
"""
from __future__ import annotations

from neo4j import GraphDatabase

from M3_KnowledgeGraph.src.models import GraphInput
from M3_KnowledgeGraph.src.ontology import NODE_LABELS

# 跨会议共享、需要唯一化合并的列表属性（按 label）
_MERGE_LIST_PROPS = {
    "Project": ["aliases", "source_names", "m2_entity_ids"],
}

# 标量属性在 ON MATCH 时不覆盖（首次入图的值为准），列表属性做并集
_SCALAR_CREATE_ONLY: set[str] = {"resolved_by_m2"}


class Neo4jStore:
    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database

    def close(self) -> None:
        self.driver.close()

    # ---------------- schema ----------------

    def ensure_schema(self) -> None:
        stmts = [f"CREATE CONSTRAINT m3_{label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                 for label in NODE_LABELS]
        stmts += [
            "CREATE INDEX m3_project_name IF NOT EXISTS FOR (n:Project) ON (n.canonical_name)",
            "CREATE INDEX m3_person_name IF NOT EXISTS FOR (n:Person) ON (n.name)",
            "CREATE INDEX m3_dept_name IF NOT EXISTS FOR (n:Department) ON (n.name)",
            "CREATE INDEX m3_doc_date IF NOT EXISTS FOR (n:SourceDocument) ON (n.meeting_date)",
            "CREATE INDEX m3_item_title IF NOT EXISTS FOR (n:MeetingItem) ON (n.title)",
        ]
        with self.driver.session(database=self.database) as s:
            for stmt in stmts:
                s.run(stmt)

    # ---------------- ingest ----------------

    def ingest(self, graph: GraphInput) -> dict:
        """幂等写入一份 GraphInput。返回写入统计。"""
        with self.driver.session(database=self.database) as s:
            with s.begin_transaction() as tx:
                node_count = 0
                for n in graph.nodes:
                    self._merge_node(tx, graph, n.label, n.node_id, n.props)
                    node_count += 1
                edge_count = 0
                for e in graph.edges:
                    self._merge_edge(tx, e.rel_type, e.src_label, e.src_id, e.dst_label, e.dst_id)
                    edge_count += 1
        return {"nodes_touched": node_count, "edges_touched": edge_count}

    def _merge_node(self, tx, graph: GraphInput, label: str, node_id: str, props: dict) -> None:
        meeting_date = graph.meeting_date
        doc_id = graph.source_document_id
        clean = {k: v for k, v in props.items() if v is not None}
        merge_lists = _MERGE_LIST_PROPS.get(label, [])

        create_props = dict(clean)
        match_props = {k: v for k, v in clean.items() if k not in merge_lists and k not in _SCALAR_CREATE_ONLY}
        params = {
            "id": node_id,
            "meeting_date": meeting_date,
            "doc_id": doc_id,
            "create_props": create_props,
            "match_props": match_props,
            **{f"{k}_new": clean.get(k, []) for k in merge_lists},
        }

        query = f"""
        MERGE (n:{label} {{id: $id}})
        ON CREATE SET n += $create_props,
                      n.first_seen_meeting_date = $meeting_date,
                      n.last_seen_meeting_date = $meeting_date,
                      n.meeting_dates = [$meeting_date],
                      n.source_document_ids = [$doc_id]
        ON MATCH SET  n += $match_props,
                      n.last_seen_meeting_date = $meeting_date,
                      n.meeting_dates = coalesce(n.meeting_dates, []) + [x IN [$meeting_date] WHERE NOT x IN coalesce(n.meeting_dates, [])],
                      n.source_document_ids = coalesce(n.source_document_ids, []) + [x IN [$doc_id] WHERE NOT x IN coalesce(n.source_document_ids, [])]
        """
        # 列表属性（如 Project.aliases/m2_entity_ids）跨会议唯一化合并
        if merge_lists:
            extra = ",\n          ".join(
                f"n.{k} = coalesce(n.{k}, []) + [x IN ${k}_new WHERE NOT x IN coalesce(n.{k}, [])]"
                for k in merge_lists
            )
            query = query.rstrip() + ",\n          " + extra + "\n        "

        tx.run(query, **params)

    def _merge_edge(self, tx, rel_type: str, src_label: str, src_id: str, dst_label: str, dst_id: str) -> None:
        query = f"""
        MATCH (a:{src_label} {{id: $src_id}}), (b:{dst_label} {{id: $dst_id}})
        MERGE (a)-[:{rel_type}]->(b)
        """
        tx.run(query, src_id=src_id, dst_id=dst_id)

    # ---------------- queries ----------------

    def _run(self, query: str, **params) -> list[dict]:
        with self.driver.session(database=self.database) as s:
            return [dict(r) for r in s.run(query, **params)]

    def stats(self) -> dict:
        nodes = self._run("MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC",
                          labels=list(NODE_LABELS))
        edges = self._run(
            "MATCH ()-[r]->() WHERE type(r) IN $types RETURN type(r) AS rel, count(*) AS c ORDER BY c DESC",
            types=["HAS_ITEM", "BELONGS_TO", "UNDER_SECTION", "DELIVERED_BY", "ABOUT_PROJECT",
                   "ASSIGNED_TO", "ALIAS_OF", "UNDER_DEPARTMENT", "IN_SECTION", "IN_DEPARTMENT"],
        )
        docs = self._run("MATCH (d:SourceDocument) RETURN d.name AS doc, d.meeting_date AS date, d.validation_status AS status, d.item_count AS items ORDER BY d.meeting_date")
        return {"nodes": nodes, "edges": edges, "documents": docs}

    def query_project(self, name: str) -> dict:
        """项目总览：canonical、alias、关联 item、出现过的会议、负责人。"""
        proj = self._run(
            """
            MATCH (p:Project)
            WHERE p.canonical_name = $name
               OR EXISTS { MATCH (:ProjectAlias {name: $name})-[:ALIAS_OF]->(p) }
            RETURN p.id AS id, p.canonical_name AS canonical_name,
                   p.aliases AS aliases, p.m2_entity_ids AS m2_entity_ids,
                   p.meeting_dates AS meeting_dates
            """,
            name=name,
        )
        if not proj:
            return {"matched": False, "project": None}
        pid = proj[0]["id"]
        items = self._run(
            """
            MATCH (p:Project {id: $pid})<-[:ABOUT_PROJECT]-(i:MeetingItem)<-[:HAS_ITEM]-(d:SourceDocument)
            RETURN d.meeting_date AS meeting_date, i.title AS title, i.item_type AS item_type,
                   i.merged AS merged
            ORDER BY d.meeting_date, i.item_index
            """,
            pid=pid,
        )
        persons = self._run(
            """
            MATCH (p:Project {id: $pid})<-[:ABOUT_PROJECT]-(:MeetingItem)-[:ASSIGNED_TO]->(per:Person)
            RETURN DISTINCT per.name AS name ORDER BY name
            """,
            pid=pid,
        )
        aliases = self._run("MATCH (a:ProjectAlias)-[:ALIAS_OF]->(:Project {id: $pid}) RETURN a.name AS name, a.kind AS kind ORDER BY name", pid=pid)
        return {"matched": True, "project": proj[0], "items": items, "persons": persons, "aliases": aliases}

    def query_person(self, name: str) -> list[dict]:
        return self._run(
            """
            MATCH (per:Person {name: $name})<-[:ASSIGNED_TO]-(i:MeetingItem)<-[:HAS_ITEM]-(d:SourceDocument)
            OPTIONAL MATCH (i)-[:ABOUT_PROJECT]->(p:Project)
            RETURN d.meeting_date AS meeting_date, i.title AS title, p.canonical_name AS project
            ORDER BY d.meeting_date
            """,
            name=name,
        )

    def query_department(self, name: str) -> list[dict]:
        return self._run(
            """
            MATCH (dep:Department {name: $name})<-[:BELONGS_TO]-(:MeetingItem)-[:ABOUT_PROJECT]->(p:Project)
            RETURN DISTINCT p.canonical_name AS project ORDER BY project
            """,
            name=name,
        )

    def query_delivery_group(self, name: str) -> list[dict]:
        return self._run(
            """
            MATCH (dg:DeliveryGroup {name: $name})<-[:DELIVERED_BY]-(:MeetingItem)-[:ABOUT_PROJECT]->(p:Project)
            RETURN DISTINCT p.canonical_name AS project ORDER BY project
            """,
            name=name,
        )

    def query_item_evidence(self, item_id: str) -> list[dict]:
        return self._run(
            """
            MATCH (i:MeetingItem {id: $id})
            RETURN i.title AS title, i.evidence_text AS evidence_text, i.evidence_json AS evidence_json,
                   i.source_indexes AS source_indexes, i.merged AS merged,
                   i.evidence_mode AS evidence_mode, i.title_source AS title_source
            """,
            id=item_id,
        )
