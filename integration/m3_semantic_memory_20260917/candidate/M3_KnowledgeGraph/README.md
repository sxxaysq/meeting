# M3_KnowledgeGraph

`meeting` 项目的**独立知识图谱支线**。

```text
M1 Extraction
      ↓
M2 SemanticConsolidator
      ├────────→  M3 Knowledge Graph   （本模块）
      │
      └────────→  M6 Task Manager
```

> **M3 暂时不参与 M6，不影响主系统运行。** 后期用于 RAG / GraphRAG、项目关系查询、
> 人员/部门/项目关联分析、历史会议追踪。

---

## 三条铁律

1. **M2 是当前会议事实的权威来源，M3 不重新解释事实。**
   M2 JSON 是事实源，Graphiti/Neo4j 只是图谱工具，不是第二个 M2。
   M3 不重做 alias 判断、Item 合并、block/generic 差异判断、项目归一、CREATE/UPDATE、部门识别。
2. **MeetingItem 不是 Task。** Task 生命周期留给 M6，本模块禁止创建正式 Task 节点。
3. **第一版优先保证图结构正确、幂等、可追溯**，再考虑 GraphRAG/RAG 和复杂推理。

---

## 技术栈

| 组件 | 版本/说明 |
| --- | --- |
| Python | `/home/yty/m1x_venv/bin/python`（3.11） |
| 图存储 | Neo4j 5.26.29 community，`bolt://127.0.0.1:7687` |
| 时间图谱 | Graphiti 0.29.x（可选层，episode/时间/后续 hybrid retrieval） |
| LLM | vLLM `http://192.168.30.215:8000/v1`，`Qwen/Qwen3.6-35B-A3B`，`enable_thinking=false` |
| Schema 校验 | jsonschema + referencing（draft-07） |

**数据流（确定性映射，无 LLM 自由抽取）：**

```text
M2 structured JSON
        ↓
M3 deterministic mapping （graph_mapper，纯函数）
        ↓
固定 Ontology
        ↓
Neo4j（幂等 MERGE）   +   （可选）Graphiti episode
```

---

## 目录结构

```text
M3_KnowledgeGraph/
├── README.md                     # 本文档
├── HANDOVER.md                   # 交接文档（环境/部署/运维/排障）
├── src/
│   ├── cli.py                    # 命令行入口
│   ├── pipeline.py               # adapter→mapper→store 编排
│   ├── models.py                 # GraphInput/NodePlan/EdgePlan + 异常
│   ├── ontology.py               # 固定 Ontology（节点/关系冻结）
│   ├── id_generator.py           # 稳定节点身份（幂等核心）
│   ├── m2_adapter.py             # M2 唯一入口：校验 + 质量门禁
│   ├── graph_mapper.py           # M2 → GraphInput 确定性映射
│   ├── neo4j_store.py            # 幂等 MERGE 写入 + 固定查询
│   ├── graphiti_client.py        # Graphiti 时间/检索层（可选）
│   └── validator.py              # 入图后一致性校验
├── schemas/
│   ├── m2_output.schema.json     # M2 输出契约（冻结副本）
│   └── graph_input.schema.json   # M3 输入信封（$ref 引用 m2_output）
├── tests/                        # pytest 单元测试（13 用例）
└── eval/
    └── poc_graphiti.py           # Graphiti + Qwen 兼容 POC
```

---

## 固定 Ontology（第一版冻结）

节点（8）：

```text
Department  WorkSection  DeliveryGroup  Project  ProjectAlias  Person  MeetingItem  SourceDocument
```

关系（10）：

```text
(SourceDocument)-[:HAS_ITEM]->(MeetingItem)
(MeetingItem)-[:BELONGS_TO]->(Department)
(MeetingItem)-[:UNDER_SECTION]->(WorkSection)
(MeetingItem)-[:DELIVERED_BY]->(DeliveryGroup)
(MeetingItem)-[:ABOUT_PROJECT]->(Project)
(MeetingItem)-[:ASSIGNED_TO]->(Person)
(ProjectAlias)-[:ALIAS_OF]->(Project)
(WorkSection)-[:UNDER_DEPARTMENT]->(Department)
(DeliveryGroup)-[:IN_DEPARTMENT]->(Department)
(DeliveryGroup)-[:IN_SECTION]->(WorkSection)
```

> **禁止创建 Task 节点**。`MeetingItem != Task`。

alias 采用独立节点方案（`ProjectAlias -[:ALIAS_OF]-> Project`），便于后续追踪每个 alias 的来源会议；
同时 `Project` 节点冗余保存 `aliases[]` / `source_names[]` / `m2_entity_ids[]` 便于直查。

---

## 稳定节点身份（幂等）

| 节点 | 身份来源 |
| --- | --- |
| `Project` | `sha1("project|" + 归一化 canonical_name)` |
| `ProjectAlias` | `sha1(project_id + 归一化 alias)` |
| `Department` | `sha1("dept|" + 归一化部门名)` |
| `WorkSection` | `sha1("section|" + 归一化部门名 + 归一化板块名)` |
| `DeliveryGroup` | `sha1("dg|" + 归一化部门名 + 归一化交付组名)` |
| `Person` | `sha1("person|" + 归一化人名)` |
| `SourceDocument` | `doc:m3:<source_document_id>` |
| `MeetingItem` | `sha1(source_document_id + "#" + 排序后的 merge_trace.source_indexes)` |

要点：

- `Project` 用 **canonical 名** 而非 M2 `entity_id` 作全局身份（`entity_id` 是 per-meeting 的 `P0001..`，跨会议不稳定），
  `entity_id` 只作为 provenance 属性 `m2_entity_ids[]` 保存。
- 全部节点 `MERGE by id` + 唯一约束兜底 → **同一 M2 输出重复导入不产生重复业务节点**。
- 名称归一只做 Unicode NFKC + 空白压缩，**不做同义判断**（那是 M2 的事）。

---

## 环境准备

### 1. Neo4j

已部署在 `/home/yty/neo4j`（用户态，非 root，用 `java` 直接启动）。运维：

```bash
/home/yty/neo4j/neo4j_ctl.sh status   # 查看
/home/yty/neo4j/neo4j_ctl.sh start    # 启动（127.0.0.1:7687 bolt / 7474 http）
/home/yty/neo4j/neo4j_ctl.sh stop     # 停止
```

认证默认：`neo4j / YOUR_PASSWORD`（可用环境变量覆盖，见下）。

### 2. 环境变量（全部有默认值，可按需覆盖）

```bash
export M3_NEO4J_URI="bolt://127.0.0.1:7687"
export M3_NEO4J_USER="neo4j"
export M3_NEO4J_PASSWORD="your-password"
export M3_LLM_BASE_URL="http://192.168.30.215:8000/v1"   # 仅 --graphiti-episode 需要
export M3_LLM_MODEL="Qwen/Qwen3.6-35B-A3B"
```

---

## CLI 使用

所有命令都在仓库根目录 `/home/yty/m1x/meeting-m2-work` 下运行。

### 入图（ingest）

```bash
/home/yty/m1x_venv/bin/python -m M3_KnowledgeGraph.src.cli ingest \
  M2_SemanticConsolidator/out_m2_generic/run0/2026-04-13.m2.json \
  --source-document "2026.4.13信息公司周例会工作安排备忘录.pdf" \
  [--meeting-date 2026-04-13] \
  [--force] [--graphiti-episode] [--no-verify]
```

- `--meeting-date`：缺省从文件名中的 `YYYY-MM-DD` 派生，不让 LLM 猜日期。
- `--source-document`：源文档稳定 ID，缺省用文件名主干。
- `--force`：实验性允许 REVIEW/ERROR 入图（全部节点打 `m3_ingest_mode=forced`）。
- `--graphiti-episode`：同时把该文档记录为 Graphiti episode（时间/检索层，可选）。
- 默认在入图后自动做一致性校验（`--no-verify` 跳过）。

**质量门禁**：默认仅 `validation.status == PASS` 可正式入图；REVIEW/ERROR 拒绝并返回退出码 3。

### 统计（stats）

```bash
/home/yty/m1x_venv/bin/python -m M3_KnowledgeGraph.src.cli stats
```

### 查询

```bash
... cli query-project "红沙泉二矿智能化建设项目"      # 支持 canonical 或 alias
... cli query-person "张三"
... cli query-department "智能矿山事业部"
... cli query-delivery-group "榆林交付组"
... cli query-evidence "item:m3:sha1:6f2b892ebe5d82ea"
```

---

## 7 类必验证查询（均已实测）

以真实 PASS 文件 `out_m2_generic/run0/2026-04-13.m2.json` 入图后：

| # | 问题 | 命令 | 实测结果 |
| --- | --- | --- | --- |
| 1 | 某项目关联哪些 MeetingItem | `query-project 红沙泉二矿智能化建设项目` | 返回 6 条 item |
| 2 | 某项目出现在哪些会议 | 同上（`meeting_dates`） | `["2026-04-13"]` |
| 3 | 某负责人负责过哪些项目/事项 | `query-person 张三` | 本数据集无 assignee，返回 `[]`（机制正常） |
| 4 | 某部门有哪些项目 | `query-department 智能矿山事业部` | 16 个项目 |
| 5 | 某交付组负责哪些项目 | `query-delivery-group 榆林交付组` | `423 项目`、`大柳塔项目` |
| 6 | 某 MeetingItem 的原始 evidence | `query-evidence <item_id>` | 原文逐字 + 完整坐标（start_char/end_char/page） |
| 7 | 某 canonical Project 有哪些 alias | `query-project 红沙泉二矿项目`（用 alias 反查） | 解析到 canonical，返回 alias 列表 |

真实入图规模（该文件）：`total_nodes=343, total_edges=481`，
其中 `MeetingItem=138, Project=91, ProjectAlias=93, Department=16, DeliveryGroup=4, SourceDocument=1`。

---

## 单元测试

```bash
cd /home/yty/m1x/meeting-m2-work
/home/yty/m1x_venv/bin/python -m pytest M3_KnowledgeGraph/tests -q
```

覆盖 11 项测试重点 + forced 标记（共 13 用例，全部通过）：
PASS 正常导入（合成 + 真实文件）、REVIEW/ERROR 默认拒绝、重复导入幂等、
同 canonical 不重复 Project、alias 绑定 canonical、Person 复用、provenance 可追溯、
evidence 原样保存、null work_section/delivery_group、block/generic 均可导入、forced 全节点标记。

> 注意：测试会清空 M3 业务标签节点以保证隔离。跑完测试后如需恢复演示图，
> 重新执行一次上面的 `ingest` 命令即可。

---

## Graphiti 集成（可选层）

- **定位**：M2 JSON 是事实源；确定性图结构由 `neo4j_store` 直写 Cypher 保证。
  Graphiti 只承担 structured JSON episode 留存、时间语义、后续 hybrid retrieval，
  **不产出 M3 正式业务节点、绝不做第二遍实体抽取覆盖 M2 结论**。
- **兼容性处理（POC 已验证，见 `eval/poc_graphiti.py`）**：
  1. vLLM 无 Responses API → 结构化输出改走 `chat.completions + response_format`（子类化 `OpenAIClient`）；
  2. Qwen3.x 必须注入 `extra_body={"chat_template_kwargs": {"enable_thinking": false}}`；
  3. vLLM 无 embeddings 接口 → 默认用确定性 `HashEmbedder`（可替换真实 embedding 服务）。

---

## 禁止事项（本阶段）

不修改 M1/M2；不改 M2 项目归一结果；不重新合并 Item；不判断 CREATE/UPDATE；
不创建正式 Task 生命周期；不让 M3 参与 M6；不用 LLM 自由发明实体/关系类型；
不用 LLM 重写 evidence；不改 evidence 字符坐标；不因 Graphiti 要求污染 M2 Schema；不使用 Dify。
