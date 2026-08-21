# M3_KnowledgeGraph 交接文档

> 面向下一位接手 M3 的工程师。先读 `README.md` 了解"是什么/怎么用"，本文讲"怎么部署的、踩过什么坑、接下来怎么扩展"。

---

## 1. 交付物清单

| # | 交付物 | 位置 | 状态 |
| --- | --- | --- | --- |
| 1 | 可运行代码 | `M3_KnowledgeGraph/src/` | ✅ |
| 2 | Graphiti + Neo4j 最小方案 | `src/neo4j_store.py`（确定性直写）+ `src/graphiti_client.py`（可选时间层）+ `eval/poc_graphiti.py` | ✅ POC 验证通过 |
| 3 | 固定 Ontology | `src/ontology.py` | ✅ 冻结 |
| 4 | M2 Adapter（校验+门禁） | `src/m2_adapter.py` | ✅ |
| 5 | 幂等 ID 方案 | `src/id_generator.py` | ✅ |
| 6 | CLI | `src/cli.py` | ✅ |
| 7 | 单元测试（13 用例） | `tests/` | ✅ 全过 |
| 8 | 真实 M2 JSON 入图演示 | `out_m2_generic/run0/2026-04-13.m2.json` 已入图 | ✅ |
| 9 | 节点/边统计 | `cli stats` | ✅ |
| 10 | 7 类查询示例 | 见 README §7 类必验证查询 | ✅ 实测 |
| 11 | README | `README.md` | ✅ |
| 12 | 交接文档 | 本文 | ✅ |

---

## 2. 运行环境（192.168.30.214，user: yty）

| 项 | 值 |
| --- | --- |
| Python | `/home/yty/m1x_venv/bin/python`（3.11） |
| 工作仓库 | `/home/yty/m1x/meeting-m2-work/` |
| Neo4j | `/home/yty/neo4j`（5.26.29 community，用户态部署），`bolt://127.0.0.1:7687` / `http://127.0.0.1:7474` |
| Java | `/home/yty/java_env`（conda-forge OpenJDK 21） |
| Neo4j 认证 | `neo4j / m3graph2026` |
| LLM | vLLM `http://192.168.30.215:8000/v1`，`Qwen/Qwen3.6-35B-A3B` |

已安装关键依赖（venv 内）：

```text
graphiti-core==0.29.3   neo4j==6.2.0        jsonschema==4.26.0
referencing==0.37.0    httpx==0.28.1       pytest==9.1.1
pydantic==2.13.4       openai==3.1.0
```

---

## 3. Neo4j 部署踩坑记录（重要）

本环境**无 root、无 Docker 权限、官方下载被 CDN 拦截**，因此采用"用户态 .deb 解包 + java 直启"方案：

1. **下载**：`dist.neo4j.org` 官方 tarball 返回 403（CloudFront 拦截）；多个 Docker 镜像源也不可用。
   最终发现机器已配置 apt 源，从 `https://debian.neo4j.com/dists/stable/5/binary-amd64/` 下载
   `neo4j_5.26.29_all.deb`，用 `dpkg -x` 解包到 `/home/yty/neo4j`（无需 root 安装）。
2. **Java**：`conda create` 默认渠道报 ToS 错误，改用 `--override-channels -c conda-forge` 安装 OpenJDK 21 到 `/home/yty/java_env`。
3. **启动**：deb 自带的 `bin/neo4j` 脚本**硬编码 `/etc/neo4j`** 路径，本环境不存在会报
   `Missing xml file for /etc/neo4j/user-logs.xml`。**解决**：绕过启动脚本，用 `java` 直接启动
   `org.neo4j.server.CommunityEntryPoint`，并用环境变量 `NEO4J_CONF` 指向 `conf/`（注意：`--config-dir` 参数反而触发错误，必须用 `NEO4J_CONF`；`--home-dir` 必填；不接受 `console` 子命令，用 `--console-mode=true`）。
4. **conf 修正**：`conf/neo4j.conf` 里 `server.logs.config` / `server.logs.user.config` 已从
   `/etc/neo4j/*.xml` 改为相对路径 `conf/*.xml`，并把目录项改成相对路径，彻底去除 `/etc/neo4j` 依赖。

**日常运维一律用 `/home/yty/neo4j/neo4j_ctl.sh`**（`start/stop/status/tail`），它封装了上述 java 直启逻辑。
数据持久化在 `/home/yty/neo4j/data/`，重启不丢失。

---

## 4. 快速复现演示

```bash
cd /home/yty/m1x/meeting-m2-work

# 0) 确保 Neo4j 在跑
/home/yty/neo4j/neo4j_ctl.sh status || /home/yty/neo4j/neo4j_ctl.sh start

# 1) 入图（仅 PASS 可正式入图；已验证幂等，重复跑不会产生重复节点）
/home/yty/m1x_venv/bin/python -m M3_KnowledgeGraph.src.cli ingest \
  M2_SemanticConsolidator/out_m2_generic/run0/2026-04-13.m2.json \
  --source-document "2026.4.13信息公司周例会工作安排备忘录.pdf"

# 2) 统计
/home/yty/m1x_venv/bin/python -m M3_KnowledgeGraph.src.cli stats

# 3) 查询
/home/yty/m1x_venv/bin/python -m M3_KnowledgeGraph.src.cli query-project "红沙泉二矿项目"

# 4) 单元测试（注意：会临时清空 M3 标签节点，跑完按第 1 步重新入图即可恢复演示）
/home/yty/m1x_venv/bin/python -m pytest M3_KnowledgeGraph/tests -q
```

当前 M2 输出中**只有 `out_m2_generic/run{0,1,2}/2026-04-13.m2.json` 三份是 PASS**；
所有 `out_m2_block/*` 均为 REVIEW，无 ERROR 样例（ERROR 用合成数据在单测中覆盖）。

---

## 5. 已知坑 & 排障速查

| 现象 | 原因 | 修复 |
| --- | --- | --- |
| `graphiti` import 报 `No module named httpx` | 依赖缺失 | `pip install httpx` |
| Graphiti 初始化 `Missing credentials OPENAI_API_KEY` | 默认 reranker 需要 key | 显式传 `cross_encoder=OpenAIRerankerClient(config=..., client=llm)` |
| Qwen 返回 `content: null` | Qwen3.x 默认开启 thinking | 请求体注入 `extra_body={"chat_template_kwargs":{"enable_thinking":false}}` |
| vLLM 报 Responses API 不支持 | Graphiti `OpenAIClient` 默认走 Responses API | 子类化改走 `chat.completions + response_format`（见 `graphiti_client.py`） |
| vLLM 无 embeddings 接口 | vLLM 未开 embedding | 用确定性 `HashEmbedder` 兜底（可换真实 embedding 服务） |
| `AddEpisodeResults has no attribute extracted_nodes` | Graphiti 0.29 字段名变更 | 用 `results.nodes` / `results.edges` |
| Cypher `size((i)<-[:HAS_ITEM]-())` 报错 | Neo4j 5 禁止 pattern 进 `size()` | 改 `COUNT { (i)<-[:HAS_ITEM]-() }` |
| schema 校验 `PointerToNowhere: /definitions/item` | 内嵌 M2 schema 的 `$ref` 解析错乱 | `graph_input.schema.json` 改为 `$ref` 引用独立的 `m2_output.schema.json`，用 `referencing.Registry` 注册二者 |

---

## 6. 架构边界（务必保持）

- **M2 JSON 是事实源**。`graph_mapper` 是纯函数确定性映射，无 LLM；同一输入必得同一计划。
- **正式业务节点只由 `neo4j_store` 直写 Cypher 产生**。Graphiti 只做 episode/时间/检索，不产出正式业务节点、不做第二遍抽取。
- **MeetingItem ≠ Task**。Task 生命周期归 M6，禁止在 M3 建正式 Task 节点。
- block/generic 不做两套逻辑，`source_mode` 仅作 provenance。
- evidence 逐字保存（`evidence_text` + 完整坐标 `evidence_json`），绝不改写。

---

## 7. 后续扩展点

1. **GraphRAG / RAG**：当前 `HashEmbedder` 只是占位。接入真实 embedding 服务后，可用
   Graphiti `hybrid_search` 做检索；`--graphiti-episode` 已能把文档写入时间层。
2. **接入更多会议**：逐份 `ingest` 历史 M2 输出即可。跨会议共享实体（Project/Person/Department）
   靠稳定 id 自动复用；`Project.meeting_dates[]` / `source_document_ids[]` 会累积，可直接回答
   "某项目出现在哪几次会议"。
3. **M6 交接**：M6 确定 Task 身份后，可在图上补 `Task` 节点并用 `MeetingItem → Task` 关系回填；
   届时仍不改 M3 现有 MeetingItem 语义。
4. **真实 embedding + 真实 reranker**：`graphiti_client.py` 的 `HashEmbedder` 与 chat reranker 可替换。
5. **多文档/多 run 治理**：目前 `source_document_id` 用文件名，若同一会议有 run0/run1/run2 多份，
   入图时应显式区分 `--source-document`，避免把不同 run 当同一文档。

---

## 8. 当前图数据库状态说明

`stats` 只统计 M3 业务标签节点。数据库里另有少量 Graphiti POC 产生的节点（`Episodic`/`Entity` 等，
Graphiti 自有标签空间），与 M3 查询互不干扰；如需清除可手动 `MATCH` 对应标签 `DETACH DELETE`。
