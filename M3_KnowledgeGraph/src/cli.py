"""M3 CLI。

示例：
    python -m M3_KnowledgeGraph.src.cli ingest out_m2/2026-04-07.m2.json \
        --meeting-date 2026-04-07 \
        --source-document "2026.4.7信息公司周例会工作安排备忘录.pdf"
    python -m M3_KnowledgeGraph.src.cli stats
    python -m M3_KnowledgeGraph.src.cli query-project "红沙泉二矿项目"
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from M3_KnowledgeGraph.src import pipeline, validator
from M3_KnowledgeGraph.src.models import M2AdapterError, M2RejectedError
from M3_KnowledgeGraph.src.neo4j_store import Neo4jStore


def _store() -> Neo4jStore:
    return Neo4jStore(
        uri=os.environ.get("M3_NEO4J_URI", "bolt://127.0.0.1:7687"),
        user=os.environ.get("M3_NEO4J_USER", "neo4j"),
        password=os.environ.get("M3_NEO4J_PASSWORD", "YOUR_PASSWORD"),
    )


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_ingest(args) -> int:
    store = _store()
    try:
        result = pipeline.ingest_m2_file(
            store,
            args.m2_json,
            meeting_date=args.meeting_date,
            source_document=args.source_document,
            force=args.force,
            graphiti_episode=args.graphiti_episode,
        )
        if not args.no_verify:
            envelope, _ = pipeline.load_and_gate(
                args.m2_json, args.meeting_date, args.source_document, force=args.force
            )
            issues = validator.validate_document(store, envelope)
            result["validation_issues"] = issues
            result["validation_ok"] = not issues
        _print(result)
        return 0 if result.get("validation_ok", True) else 2
    except M2RejectedError as e:
        print(f"[拒绝] {e}", file=sys.stderr)
        return 3
    except M2AdapterError as e:
        print(f"[输入错误] {e}", file=sys.stderr)
        return 4
    finally:
        store.close()


def cmd_stats(args) -> int:
    store = _store()
    try:
        _print(store.stats())
        return 0
    finally:
        store.close()


def cmd_query_project(args) -> int:
    store = _store()
    try:
        res = store.query_project(args.name)
        _print(res)
        return 0 if res.get("matched") else 1
    finally:
        store.close()


def cmd_query_person(args) -> int:
    store = _store()
    try:
        _print(store.query_person(args.name))
        return 0
    finally:
        store.close()


def cmd_query_department(args) -> int:
    store = _store()
    try:
        _print(store.query_department(args.name))
        return 0
    finally:
        store.close()


def cmd_query_delivery_group(args) -> int:
    store = _store()
    try:
        _print(store.query_delivery_group(args.name))
        return 0
    finally:
        store.close()


def cmd_query_evidence(args) -> int:
    store = _store()
    try:
        _print(store.query_item_evidence(args.item_id))
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="m3", description="M3_KnowledgeGraph CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="导入一份 M2 输出（仅 PASS 可正式入图）")
    pi.add_argument("m2_json")
    pi.add_argument("--meeting-date", default=None, help="YYYY-MM-DD；缺省从文件名派生")
    pi.add_argument("--source-document", default=None, help="源文档稳定 ID（如 PDF 文件名）")
    pi.add_argument("--force", action="store_true",
                    help="实验性：允许 REVIEW/ERROR 入图（全部节点打 forced 标记）")
    pi.add_argument("--graphiti-episode", action="store_true",
                    help="同时把该文档记录为 Graphiti episode（时间/检索层，可选）")
    pi.add_argument("--no-verify", action="store_true", help="跳过入图后一致性校验")
    pi.set_defaults(func=cmd_ingest)

    ps = sub.add_parser("stats", help="图谱节点/边/文档统计")
    ps.set_defaults(func=cmd_stats)

    pq = sub.add_parser("query-project", help="查询项目：关联 item/会议/负责人/alias")
    pq.add_argument("name")
    pq.set_defaults(func=cmd_query_project)

    pp = sub.add_parser("query-person", help="查询负责人参与的事项/项目")
    pp.add_argument("name")
    pp.set_defaults(func=cmd_query_person)

    pd = sub.add_parser("query-department", help="查询部门名下项目")
    pd.add_argument("name")
    pd.set_defaults(func=cmd_query_department)

    pg = sub.add_parser("query-delivery-group", help="查询交付组名下项目")
    pg.add_argument("name")
    pg.set_defaults(func=cmd_query_delivery_group)

    pe = sub.add_parser("query-evidence", help="按 MeetingItem id 查原始 evidence")
    pe.add_argument("item_id")
    pe.set_defaults(func=cmd_query_evidence)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
