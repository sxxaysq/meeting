"""Live probe: 红沙泉 family collapse, 乌冬→乌东 typo, cross-family refusal.

Uses real embedding and model services, and resets the configured semantic
memory before probing. Use an isolated test Neo4j database. Not an automated
unit test or part of the chronological batch run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parent / "candidate"
sys.path.insert(0, str(CANDIDATE))

from M3_KnowledgeGraph.src.embedding_client import EmbeddingClient  # noqa: E402
from M3_KnowledgeGraph.src.project_resolver import ProjectResolver  # noqa: E402
from M3_KnowledgeGraph.src.semantic_memory import connect_memory  # noqa: E402
from M6_TaskManager.src.llm_client import OpenAICompatibleLifecycleClient  # noqa: E402

DOC = "E2E-PROBE"

FAMILY_VARIANTS = [
    "红沙泉二矿智能化建设项目",
    "红沙泉现场",
    "红沙泉项目人员定位及运输管控系统采购项目",
    "红沙泉项目地测采保障系统项目",
    "红沙泉项目无人机综合管控平台采购项目",
    "红沙泉项目路面平整度监测系统采购项目",
    "红沙泉二矿项目智慧档案馆采购项目",
    "红沙泉二矿项目智能加水及仓储物资采购项目",
    "红沙泉二矿项目智能综合管控平台技术服务采购项目",
    "国能新疆矿业红沙泉二矿智能化建设服务项目地测采保障系统项目（2）",
    "国能新疆矿业红沙泉二矿智能化建设服务项目矿区火灾监测与预警系统",
    "国能新疆矿业红沙泉二矿智能化建设服务项目综合环境监测系统",
    "国能新疆矿业红沙泉二矿智能化建设服务项目视频分析系统",
    "国能新疆矿业红沙泉二矿智能化建设服务项目路面平整度监测系统采购项目（2）",
]

OUTSIDERS = ["陶忽图煤矿智能化项目", "赵石畔项目", "大海则项目", "宁煤智能化洗煤厂项目"]

TYPO_CONTEXT = {
    "title": "计划采购评审6项",
    "department": "经营管理部",
    "content": (
        "计划采购评审6 项，其中公开招标2 项：序号采购方式采购名称预算金额（万元）开标日期"
        "1公开招标乌冬项目无人化综放工作面规划截割自适应精准协同控制移动管理系统采购60"
        "6 月25 日2公开招标自主可控操作系统及数据库推广项目数据库软件采购70"
    ),
    "evidence": {
        "text": "1公开招标乌冬项目无人化综放工作面规划截割自适应精准协同控制移动管理系统采购60"
    },
}


def context_for(name: str, extra: str = "") -> dict:
    return {
        "title": name,
        "department": "智能矿山事业部",
        "content": f"{name}{extra}推进中。",
        "evidence": {"text": name},
    }


def main() -> int:
    embedder = EmbeddingClient()
    memory = connect_memory(embedder)
    memory.reset()
    memory.ensure_schema()
    client = OpenAICompatibleLifecycleClient(
        base_url=os.getenv("M6_LLM_BASE_URL", "http://127.0.0.1:8000/v1"),
        model=os.getenv("M6_LLM_MODEL", "qwen3.8-27b"),
        enable_thinking=False,
    )
    resolver = ProjectResolver(memory, client)
    failures: list[str] = []

    def header(text: str) -> None:
        print("\n" + "=" * 78 + f"\n{text}\n" + "=" * 78)

    header("阶段 1：按时间顺序建立记忆（乌东项目在权威 project 字段多次出现，与真实数据一致）")
    seeds = [
        # 真实数据里 乌东项目 在 04-07/04-13/04-20 的权威 project 字段反复出现，
        # 到 06-22 遇到错字 乌冬项目 时已多次取证。错字改写门槛要求正确写法
        # 至少被权威字段取证两次，故此处按真实复发条件播种两次。
        ("乌东项目", "FIELD", context_for("乌东项目", "测试及调试")),
        ("乌东项目", "FIELD", context_for("乌东项目", "无人化综放工作面推进")),
        ("红沙泉项目", "FIELD", context_for("红沙泉项目", "现场跟进")),
        ("红沙泉二矿项目", "FIELD", context_for("红沙泉二矿项目", "集控中心装修")),
        ("陶忽图项目", "FIELD", context_for("陶忽图项目", "投标")),
    ]
    for name, authority, context in seeds:
        result = resolver.resolve(
            name, document=DOC, authority=authority, context=context
        )
        print(
            f"  {name:<14} -> {result.canonical_name!r:<16} "
            f"status={result.status:<11} by={result.decided_by:<7} family={result.family_text}"
        )
        print(f"      reason: {result.reason[:90]}")

    header("阶段 2：红沙泉族其余 14 种真实称谓，应全部归到「红沙泉项目」")
    collapsed: list[str] = []
    escaped: list[str] = []
    for name in FAMILY_VARIANTS:
        result = resolver.resolve(
            name, document=DOC, authority="FIELD", context=context_for(name, "相关工作")
        )
        ok = result.canonical_name == "红沙泉项目"
        (collapsed if ok else escaped).append(name)
        print(
            f"  {'OK ' if ok else '!! '}{name[:44]:<46} -> "
            f"{result.canonical_name!r} [{result.status}/{result.decided_by}]"
        )
    print(f"\n  归并成功 {len(collapsed)}/{len(FAMILY_VARIANTS)}，未归并 {len(escaped)}")
    if escaped:
        failures.append(f"红沙泉族未全部归并：{escaped}")

    header("阶段 3：乌冬项目（正文表格错字，project 字段为空）应判为乌东项目")
    typo = resolver.resolve(
        "乌冬项目", document=DOC, authority="CONTENT", context=TYPO_CONTEXT
    )
    print(f"  乌冬项目 -> {typo.canonical_name!r}  status={typo.status}  by={typo.decided_by}")
    print(f"      reason: {typo.reason[:160]}")
    print("      召回候选（含权威度证据）:")
    for candidate in typo.candidates:
        print(
            f"        cos={candidate['score']:.4f} {candidate['text']:<14} "
            f"出现{candidate['occurrences']}次/字段{candidate['field_count']}次 "
            f"status={candidate['status']}"
        )
    if typo.canonical_name != "乌东项目":
        failures.append(
            f"乌冬项目未纠正为乌东项目，实得 {typo.canonical_name!r} ({typo.status})"
        )

    header("阶段 4：跨族不得误合（陶忽图 / 赵石畔 / 大海则 不能进红沙泉族）")
    wrong: list[str] = []
    for name in OUTSIDERS:
        result = resolver.resolve(
            name, document=DOC, authority="FIELD", context=context_for(name)
        )
        bad = result.canonical_name == "红沙泉项目"
        if bad:
            wrong.append(name)
        print(
            f"  {'!! ' if bad else 'OK '}{name:<22} -> {result.canonical_name!r} "
            f"[{result.status}/{result.decided_by}]"
        )
    if wrong:
        failures.append(f"跨族误合：{wrong}")

    header("阶段 5：记忆库复用——重复解析不再调用模型")
    before = resolver.stats()
    again = resolver.resolve(
        "红沙泉二矿项目", document=DOC, authority="FIELD", context=context_for("红沙泉二矿项目")
    )
    after = resolver.stats()
    print(f"  再次解析 红沙泉二矿项目 -> {again.canonical_name!r} by={again.decided_by}")
    print(
        f"  llm_calls: {before['llm_calls']} -> {after['llm_calls']}（应无新增）"
        f"   memory_hits: {before['memory_hits']} -> {after['memory_hits']}"
    )
    if after["llm_calls"] != before["llm_calls"]:
        failures.append("记忆库未命中，重复解析又调了模型")

    header("记忆库快照")
    stats = memory.stats()
    print(
        f"  projects={stats['projects']}  surfaces={stats['surfaces']}  "
        f"rules={stats['rules']}  vector_queries={stats['vector_queries']}"
    )
    print(f"  surface_status={stats['surface_status']}")
    print(f"  embedding={stats['embedding']}")
    print(f"  resolver={after}")
    print("\n  已记住的规则:")
    for rule in memory.export_snapshot()["rules"]:
        print(
            f"    [{rule['kind']:<14}] {str(rule['premise'])[:22]:<24} -> "
            f"{str(rule['conclusion'])[:22]:<24} hits={rule.get('hits')}"
        )

    header("判定：" + ("FAILED" if failures else "PASSED"))
    for failure in failures:
        print(f"  - {failure}")
    if not failures:
        print("  族归并、错字纠正、跨族隔离、记忆复用全部成立")

    memory.reset()
    memory.close()
    embedder.close()
    client.client.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
