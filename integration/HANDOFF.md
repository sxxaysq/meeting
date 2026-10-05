# 会议数据全链路项目 · 交接文档（HANDOFF）

> 单一权威交接文档，替代此前所有 HANDOFF_*/REPORT/状态报告。
> 面向下一个接手的开发者，可无人值守理解全貌并继续推进。
> 最后核对：**2026-09-21**（本轮核对内网部署与 Demo；其他模块保留原检查日期的记录）。
> 仓库根：`/home/yty-s/meeting-m2-work`；本机 `192.168.30.216`（hostname `user`）。

---

## 0. 30 秒速览

- **活的流水线是 M1→M3→M6（native，`m2_enabled=false`）**：M1 九字段条目**直接**进 M6，M3 提供语义检索/项目身份归一，**M2 语义归并已废弃**（不在活链路）。M4 招投标分离作为 M6 准入阶段的一个路由分支内联执行。
- **当前最新工作目录**：`integration/m3_semantic_memory_20260917/`，其中 **`candidate/` 是活的语义版代码**（M3/M6/m6_service 的改动都在这里），仓库根的 `M3_KnowledgeGraph/`、`M6_TaskManager/` 是**冻结原版（红线，勿改）**。
- **0917 轮核心成果**：M3 检索改为 **bge-m3 语义向量 + Neo4j 长期记忆库 + LLM 判定**，实现项目族归并（红沙泉*→红沙泉项目）、文档错字纠正（乌冬→乌东）、记忆规则复用，并**移除全部正则身份校验**。
- **0918 轮核心成果（P1 人工审核降噪，已实施并验证）**：① 1a 公司级默认部门回退（「各部门」类指令 department=null 不再强制复核，30→3）；② 2a MULTI 多目标子决策（一条 item 可下多条命令，47 条零塌缩）；③ 3 个遗留单测修复，**M6 90/90、M3 24/24 全绿**；④ 前 10 场重跑 `runs/full_semantic_v3_10` 完成（`complete=true`），Demo 已切换。
- **内网上线入口**：`http://192.168.30.216/meeting/`；17 个部门入口从“部门工作台”进入。后端仍为 `127.0.0.1:18098`，由 `meeting-taskmanager.service` 托管，开机启动、异常自动重启。当前展示 v3_10 结果及后续人工处理。
- **0920 修复**：进度/字段更新同步追加任务说明；复核页显示原因、核对事项及可点击任务名。v3_10 展示库已修复 210 个任务说明、575 条事件快照，原始 run 库不改；详见 §4.4。
- **0920 多表单复核**：展开记录后自动建议拆分子目标，分别预填更新目标/新建任务和字段；整条确认以单事务执行，可保存全部草稿。详见 §4.5。
- **模型/向量/图基础设施全部在线**；但 **M1/M2/M4/M6 的 HTTP 集成服务当前未运行**（见 §2）。

---

## 1. 项目架构（M1→M2〔废弃〕→M3→M4→M6）

### 1.1 模块职责与源码位置

| 模块 | 职责 | 冻结原版（红线） | 活的语义版（本轮改动） |
|---|---|---|---|
| **M1 信息抽取** | 会议 PDF → 九字段 items（department/work_section/delivery_group/project/item_type/assignee/title/content/evidence），坐标由代码算 | `M1_Extraction/src/` | 同左（M1 未改） |
| **M2 语义归并** | 同会议 item 保守归并 + 项目实体归一 + 质量门 | `M2_SemanticConsolidator/src/` | **废弃**：活链路 `m2_enabled=false`，M6 直接消费 M1 条目 |
| **M3 知识图谱/语义记忆** | 项目身份归一（族归并/错字纠正）+ 历史任务语义检索 + 证据语义阅读 | `M3_KnowledgeGraph/src/`（原版仅图查询/HashEmbedder 占位） | **`integration/m3_semantic_memory_20260917/candidate/M3_KnowledgeGraph/src/`**（见 §1.3） |
| **M4 招投标分离** | LLM 分类招投标条目，分流到独立收件记录 | `integration/m4_bidding/` | 内联复用：M6 准入阶段 `_route_bidding` 调 `m4_bidding/classifier.py` |
| **M6 任务管理** | 检索候选→LLM 判定生命周期动作→校验→执行→审计 | `M6_TaskManager/src/` | **`integration/m3_semantic_memory_20260917/candidate/M6_TaskManager/src/`** |

### 1.2 数据流（活链路 M1→M3→M6）

```
15 份会议 PDF（数据集/2026.<M>.<D>…备忘录.pdf）
   │  M1 抽取（:18091 /m1/extract，generic 整篇单调用）
   ▼
九字段 items  ── 权威冻结产物 ──►  M1_Extraction/out_v14/generic/*.items.json
   │  M1 入库（:18090 /m1/ingest）          integration/m1_m3_m6_20260915/frozen_m1/E2E*/m1.json（native 运行输入）
   ▼  ods_m1_source_documents(15) / ods_m1_meeting_items(2074)  [MySQL m1_staging]
   │
   │  ★ native 全链路（m2_enabled=false，M2 不参与）★
   ▼
native_full_dataset.py（编排器，按会议日期顺序）
   ├─ source_admission._route_bidding  → M4 分类 → route='M4' 招投标条目分流（ROUTE_M4）
   ├─ source_admission._resolve_projects → M3 项目身份归一：
   │      ProjectResolver = 记忆库(Neo4j) 命中 → bge-m3 向量召回候选 → qwen3.8-27b 判定
   │      族归并(红沙泉*→红沙泉项目) / 错字纠正(乌冬→乌东) / 规则写回记忆库
   ├─ M3 task_retrieval.SemanticTaskIndex → 历史任务 bge-m3 语义检索（候选）
   ├─ M6 lifecycle_judge(qwen3.8-27b) → 单 decision 或 MULTI（多目标映射明确时拆 sub_decisions）
   ├─ M6 command_validator(+SemanticReader 证据语义) → 结构校验 → executor 执行
   ▼
runs/<run>/lifecycle.sqlite（tasks/task_events/task_audit/lifecycle_reviews）
   │  export_semantic_demo.py（EXPORT_SOURCE/EXPORT_TARGET 环境变量指定）映射到 Demo schema
   ▼
M6_TaskManager_Demo（:18098 前端展示）
```

**血缘 ID**：现行 `"ITEM:"+sha256({document_id, source_indexes, start_char, end_char, content, item_type})[:24]`（早期文档的 `item:m1:`+sha1 方案已退役）。

### 1.3 M3 语义记忆层（活代码在 candidate/M3_KnowledgeGraph/src/）

| 文件 | 职责 |
|---|---|
| `embedding_client.py` | bge-m3 dense（:8000/v1/embeddings，1024 维）+ bge-reranker（:8003）客户端；线程安全缓存；传输失败抛 `EmbeddingServiceError`（不静默降级） |
| `semantic_memory.py` | **Neo4j 长期记忆库**：`M3MemProject`(规范身份)/`M3MemSurface`(观测称谓+向量+CANONICAL/ALIAS/TYPO/INDEPENDENT/UNRESOLVED)/`M3MemRule`(FAMILY_COLLAPSE/TYPO_MAP/KEEP_SEPARATE)/`M3MemSemantic`(证据语义缓存)；用 Neo4j 原生向量索引 `db.index.vector.queryNodes` 做 cosine 召回；所有标签 `M3Mem` 前缀（与共享实例的 Entity/Chunk/MilvusKB 隔离） |
| `project_resolver.py` | **项目身份归一**：记忆优先→向量召回→LLM 判定。族归并（family_text+「项目」作 standard_name）、错字纠正（靠权威度 field_count/occurrences，非字面相似度）、规则写回复用 |
| `semantic_reader.py` | LLM 阅读证据语义（完成/否定/移交/更名），替代 command_validator 的 11 条正则；结果缓存进 M3MemSemantic |
| `task_retrieval.py` | 历史任务语义检索：`SemanticTaskIndex`(bge-m3 嵌入任务)、`project_compatible`(按记忆库 family 而非矿号正则)、`same_goal`(cosine≥0.88)、`configure_semantics`(注入 embedder+family_lookup)；**已删除** scope_conflict 矿号正则、层级正则 |
| `project_memory.py` | 跨会议历史项目召回（bge-m3 cosine 排序，从记忆图补 canonical/status） |

### 1.4 M6 准入与生命周期（活代码在 candidate/M6_TaskManager/src/）

- `source_admission.py`：M4 路由 + 项目族归一（调 ProjectResolver）+ `_scan_embedded_mentions`（捕捉正文内错字）。**已移除** explicit_parent / validated_parent 限定词超集拒绝 / resolve_reference 矿号正则。
- `candidate_retriever.py`：5 行兼容 shim，re-export 自 M3 `task_retrieval`。
- `command_validator.py`：结构校验（目标在候选集/版本/状态机/证据子串/Schema）+ SemanticReader 替代措辞正则；**1a 默认部门回退**：`_route_for_create` 对 `NON_PROJECT_WORK` 且 department 为空的 CREATE 落默认部门（构造参数/env `M6_DEFAULT_DEPARTMENT`，缺省「公司级」），`changes["department_fallback"]` 留痕，主表无该部门仍 REVIEW（不静默造部门）；**2a** `build_multi`：MULTI 各子决策走全量校验，任一 BusinessAmbiguity/策略复核 → 整单塌缩为一条 REVIEW（原子）。
- `lifecycle_judge.py` + `prompts/lifecycle_judge.md` + `schemas/lifecycle_decision.schema.json`：模型只选动作/候选序号/字段名，ID/版本/路由/字段值由程序取。**2a MULTI 契约**：单 decision 或 `decision=MULTI`+`sub_decisions[]`（2–3 条，子动作禁 REVIEW/EXPAND/MULTI；任一目标归属不确定仍 REVIEW）。
- `executor.py`：**2a** `execute_multi` 单事务执行 MULTI 全部子命令 + 单条 processing_record（幂等重放不变）；子命令 provenance 打 `multi_goal_index/total`。
- `db/schema.sql`：`task_source_links` 唯一约束为 `(source_document_id, source_item_id, task_id)`（MULTI 一条 item 可扇出多任务；只影响新建库，历史 run 库只读不受影响）。

### 1.5 依赖关系

- 编排器 `native_full_dataset.py` 把 `candidate/` 加入 sys.path，import `M3_KnowledgeGraph.src.*` 与 `M6_TaskManager.src.*`。
- M6 `candidate_retriever`/`command_validator` 跨模块 import M3 `task_retrieval`（family/语义检索）。
- 运行需：qwen3.8-27b（LLM）、bge-m3（向量）、Neo4j（记忆）、neo4j Python 包（在 `/home/yty-s/venvs/m6-service`，3.11.15）。

---

## 2. 服务与端口

> 实测 2026-09-20：模型/向量/图/Demo 在线；**M1/M2/M4/M6 的 HTTP 集成服务当前未运行**，需按下方命令重启。

| 端口 | 服务 | 源码/启动 | 健康检查 | 当前 |
|---|---|---|---|---|
| **18090** | M1 入库 staging | `integration/m1_staging/start_staging.sh`（需 `M1_STAGING_PORT=18090`、`M1_STAGING_DSN`） | `/healthz` | ✗ 未运行 |
| **18091** | M1 抽取 | `integration/m1_service/app.py`（uvicorn，需 LLM_*） | `/healthz` | ✗ 未运行 |
| **18093** | M2 归并（废弃） | `integration/m2_service/start_m2_service.sh`（`M2_SERVICE_PORT`、`M2_DSN`） | `/healthz` | ✗ 未运行 |
| **8093** | M4 招投标 | `integration/m4_bidding/start_m4_service.sh`（`M4_BIDDING_DSN`） | `/healthz` | ✗ 未运行 |
| **18096** | M6 服务 | `integration/m6_service/start_m6_service.sh`（venv `/home/yty-s/venvs/m6-service`，Python 3.11） | `/healthz` | ✗ 未运行 |
| **80 /meeting/** | **内网系统入口** | 复用 `frontend-main` Nginx，配置 `/mnt/data/smartcore/frontend/nginx/main.conf` 中的 `/meeting/` 独立路径 | `/meeting/health` | ✓ 跨机器 HTTP 已验证 |
| **18098** | **M6 Demo 后端（仅本机）** | `sudo systemctl restart meeting-taskmanager.service`；环境文件 `M6_TaskManager_Demo/deploy/service.env`（0600，不输出内容） | `/health` | ✓ systemd enabled/active，数据仍为 `data/m1_m3_m6_semantic_v3_10.sqlite` |
| **8000**(本机) | bge-m3 embedding | docker `ix-bge-m3-deploy`（Iluvatar GPU），`/v1/embeddings` | POST `/v1/embeddings` | ✓ |
| **8003** | bge-reranker-v2-m3 | 同上部署，`/v1/rerank` | POST `/v1/rerank` | ✓ |
| **8002** | no_think_proxy | `integration/no_think_proxy_8002.py`（顿悟禁思考兜底，转发 .215:8000） | — | 视顿悟需要 |
| **7687/7474** | Neo4j（M3Mem 记忆库） | bolt `neo4j://127.0.0.1:7687`，账号 `neo4j/YOUR_PASSWORD`，库 `neo4j` | `http://127.0.0.1:7474` | ✓ |
| **3306** | MySQL `m1_staging` | ods_m1_*/ods_m2_*/ods_m4_* 贴源层 | — | 视服务而定 |

**上线后维护命令**（已由 systemd 托管，不再使用 kill/setsid 重复启动）：
```bash
sudo systemctl status meeting-taskmanager.service
sudo systemctl restart meeting-taskmanager.service
sudo journalctl -u meeting-taskmanager.service -n 50 --no-pager
curl -fsS http://127.0.0.1:18098/health
curl -fsS http://192.168.30.216/meeting/health
```
切换展示库：编辑 `M6_TaskManager_Demo/deploy/service.env` 中的 M6_DATABASE_PATH 后重启服务，保留 0600 权限。涉及代码或数据库迁移时先 `sudo systemctl stop meeting-taskmanager.service`，完成后 `start`；不要只杀 PID，否则 Restart=always 会拉起进程。`display-service.pid` 由 ExecStartPost 自动维护。

---

## 3. LLM 与模型服务

| 服务 | 地址 | 用途 | 关键参数 |
|---|---|---|---|
| **vLLM qwen3.8-27b** | `http://192.168.30.215:8000/v1` | M6 生命周期判定、M4 招投标分类、M3 项目归一/证据阅读 | `temperature=0`、`enable_thinking=false`；模型名 `qwen3.8-27b` |
| **bge-m3 embedding** | `http://127.0.0.1:8000/v1/embeddings` | 记忆库向量、历史任务语义检索、族/错字召回 | 1024 维；env `M3_EMBEDDING_URL`、`M3_VECTOR_DIMENSIONS=1024` |
| **bge-reranker-v2-m3** | `http://127.0.0.1:8003/v1/rerank` | 交叉编码重排（可选，当前 resolver 未强制用） | env `M3_RERANK_URL` |
| **Neo4j** | `bolt://127.0.0.1:7687` | M3Mem 长期记忆库（向量索引 + 图） | `neo4j/YOUR_PASSWORD`；env `M3_NEO4J_URI/USER/PASSWORD/DATABASE` |

**resolver 阈值/开关**（env，默认值）：`M3_FAMILY_RECALL_THRESHOLD=0.62`（族召回下限，实测红沙泉族内 0.704–0.926、最近异族陶忽图 0.505）、`M3_TYPO_RECALL_THRESHOLD=0.70`、`M3_COLLAPSE_YEARS=0`（年份默认不归并，2025/2026 视为不同年度周期）、`M3_RESOLVE_BATCH=8`、`M3_CANDIDATES_PER_NAME=6`。

**M6 开关**：`M6_DEFAULT_DEPARTMENT`（默认「公司级」）——1a 默认部门回退目标，必须在 Department Master（`departments.json`，已含该条目）中存在，否则仍 REVIEW。

> ⚠️ vLLM(.215:8000) 曾于 2026-09-17→18 隔夜宕机（端口 refused），已恢复。重跑前先 `curl http://192.168.30.215:8000/v1/models` 确认。

---

## 4. 目前完成情况（附验证数据）

### 4.1 已完成里程碑

1. **M3 语义记忆库改造**：检索从词面 dice/正则 → **bge-m3 向量 + Neo4j 记忆 + LLM 判定**；移除全部正则身份校验（矿号/井号/期次/层级/限定词超集）。
2. **红沙泉族归并**：语义结果里红沙泉 distinct 项目 **19（旧正则版）→ 1（红沙泉项目）**；记忆库记录 `FAMILY_COLLAPSE`。
3. **乌冬→乌东 错字纠正**：probe 独立验证 PASSED；Fix A 已使真实管线覆盖 M4 路由条目（v3_10 记忆库 TYPO surface 实测存在）。
4. **冷启动 bug 修复**：`_shortlist` 跳过无 canonical 未解析兄弟；CANONICAL 用族名作 standard_name。
5. **全链路 E2E（11 场，full_semantic）**：跑通 0407–0720，第 12 场因过严断言中断，Fix B 已修。
6. **P1 人工审核降噪（0918 轮，已实施并验证）**：
   - **1a 公司级默认部门回退**：`NON_PROJECT_WORK` 且 department=null 的 CREATE 路由到默认部门「公司级」（`departments.json` 已加条目，aliases 各部门/全公司/公司各部门），`department_fallback` 留痕。效果：「部门无法唯一映射:None」复核 **30 → 3**（剩余 3 条为 PROJECT_TASK dept=None，见 §5-P1）。
   - **2a MULTI 多目标子决策**：schema/判定/校验/执行/提示词全链路支持 `sub_decisions[]`（≤3、原子塌缩、单事务、幂等不变）。效果：v3_10 中 MULTI **47 条全部执行、零塌缩**（112 子命令：83 PROGRESS_UPDATE + 29 CREATE）。
   - **3 个遗留单测修复**（`tests/test_source_admission.py`，只改夹具不动生产）：按「先落地后泛称」拆断言、夹具补 `configure_semantics`/family_lookup。**M6 90/90、M3 24/24 全绿**。
7. **前 10 场重跑（runs/full_semantic_v3_10，2026-09-18 完成，`complete=true`，replay_verified=true）**：CREATE 689 / PROGRESS_UPDATE 495 / ROUTE_M4 98 / SKIP 93 / **REVIEW 65** / **MULTI 47** / COMPLETE 1。同口径 REVIEW：旧前 10 场 88 → 65（-26%）。红沙泉 04-07 族归并断言、派发全 test:// 断言通过。
8. **前端展示**：v3_10 结果导出至 `M6_TaskManager_Demo/data/m1_m3_m6_semantic_v3_10.sqlite`（10 runs / 718 任务 / 1553 事件 / 65 复核），18098 已切换。

### 4.2 关键验证数据

- **probe_resolver.py**：`PASSED`（族归并 14/14、乌冬→乌东 TYPO、跨族隔离、记忆复用 llm_calls 不增）。运行：`cd integration/m3_semantic_memory_20260917 && /home/yty-s/venvs/m6-service/bin/python probe_resolver.py`
- **单测**：M3 语义层 `tests/test_semantic_layer.py` **24/24**；M6 套件 **90/90**（含新增 `tests/test_multi_goal.py` 15 用例：1a 回退/不发明部门、schema 约束、judge 解析与修复、build_multi 塌缩、execute_multi 原子与重放、service 端到端 MULTI）。
- **v3_10 记忆库快照**（10 场后）：projects 291 / surfaces 537（CANONICAL 146 / ALIAS 222 / INDEPENDENT 165 / UNRESOLVED 3 / TYPO 1）/ rules 360。
- **v3_10 运行统计**：resolver llm_calls 74（format_retries 1）；lifecycle calls 1685（format_retries 5）；service_failures 全 0。DB 实测：tasks 718 / task_events 1297 / task_audit 1553 / lifecycle_reviews 65 / processing_records 1488 / dispatch_queue 1297。
- **剩余复核构成（65 条）**：自由文本真歧义 50（其中多目标类 30，抽查均为描述宽泛/候选覆盖不全/跨项目等真歧义）、PENDING_REVIEW_DUPLICATE 6（防重复护栏，全部命中正确，见 §5-P1 注）、部门类 3、PROJECT_IDENTITY 3、DUPLICATE_GOAL 3。

### 4.3 运行产物位置

```
integration/m3_semantic_memory_20260917/
├── candidate/                      # ★活的语义版代码（M3/M6/m6_service）★
│   └── M6_TaskManager/.bak_20260918_1a2a/   # 1a/2a 改动前原文件备份
├── runs/full_semantic/             # 11 场结果（旧，已被 v3_10 超越）
├── runs/full_semantic_v2/          # 已废弃（跑完 04-07 停止）
├── runs/full_semantic_v3_10/       # ★当前最新：前 10 场（1a/2a 生效），complete=true★
├── runs/pilot_fixed/               # 2 场冒烟（complete=true）
├── probe_resolver.py               # 独立验证探针
├── export_semantic_demo.py         # run→Demo 导出（EXPORT_SOURCE/EXPORT_TARGET 环境变量）
├── recovery_backup_20260917_142837/ # 恢复前磁盘正则版备份
└── logs/
integration/m1_m3_m6_20260915/frozen_m1/          # 冻结输入 15 场 + frozen_manifest.json
integration/m1_m3_m6_20260915_first10/frozen_m1/  # 前 10 场子集（真实副本 + 10 条 manifest）
```

---

### 4.4 2026-09-20 任务说明与复核展示修复（已验证）

- 根因：`PROGRESS_UPDATE` 原来只递增版本并写事件，Demo 导出将其映射为 `UPDATE_FIELDS`，任务说明仍取旧快照。
- 活代码 `candidate/M6_TaskManager/src/executor.py` 的共同执行路径对 `PROGRESS_UPDATE/MODIFY/TRANSFER` 保留原说明并追加本次来源内容；已有内容不重复追加。单命令与 MULTI 均覆盖，说明及 before/after 审计在同一事务内写入，幂等重放不重复追加。
- `export_semantic_demo.py` 按审计先后顺序投影旧版说明，确保历史发布只含当时已有内容；输入 `runs/full_semantic_v3_10/lifecycle.sqlite` 保持只读。现有 Demo 库仅修正 210 个任务的 description 和 575 条事件的说明快照；718 任务 / 1553 事件 / 65 pending 复核数量、证据及其他字段均保持一致。
- `M6_TaskManager_Demo/static/{app.js,index.html,style.css}`：复核原因中的“候选0/1”等根据该条记录保存的候选顺序映射为可点击任务名；展开关联任务可见复核时部门、项目、状态、说明和近期进展，点击打开当前详情及历史发布。规则码转换为可读原因，提供核对事项。缺失映射不猜测；加载失败在复核页提示。确认按钮仍仅创建新任务。
- 验证：活 M6 90 项 + 导出回归 1 项通过；Node 前端回归通过；浏览器检查全部 65 张复核卡片无候选数字或遗留 `PENDING_REVIEW_DUPLICATE`，无缺失映射、无控制台错误，任务名点击及历史发布说明逐次增长已实测。
- 备份：`integration/m3_semantic_memory_20260917/backups/bugfix_20260920_101323/`，含改前源文件、`demo_before.sqlite` 和 `receipt.json`。源文件按同相对路径恢复；数据库备份包含修复前全部展示数据。只在需要完整回退且已核对之后人工操作时使用数据库全量恢复。
- 复测：`cd integration/m3_semantic_memory_20260917/candidate/M6_TaskManager && /home/yty-s/venvs/m6-service/bin/python -m pytest tests/ ../../tests/test_demo_export.py -q`；前端可在装有 Node 的环境执行 `node M6_TaskManager_Demo/tests/test_review_ui.cjs`。
- 本次未重跑模型/15 场推理、未改冻结原版、未改真实派发、未提交推送 Git；Demo 通过静态资源和数据库事务更新生效，无需重启服务。新增推理使用已修复的 candidate 执行器。

---

### 4.5 2026-09-20 多目标复核表单（已部署验证）

- 修复原页面每条复核只能填写一个 CREATE 表单的问题。现在每条复核记录可展开多个子任务表单，每个表单选择 UPDATE_FIELDS 或 CREATE，支持增删表单、改选关联目标、保存全部草稿和统一确认。
- `M6_TaskManager_Demo/app/review_plan.py` 复用现有模型客户端，以原文、复核原因和关联任务当前状态生成建议（temperature=0，禁思考，最多一次格式修复）。先按业务目标拆分，再选最可能的任务；只采用原文连续片段作为本次说明，目标索引必须在候选范围。模型不执行写入。
- 每张更新表单预填目标当前标题、部门、项目、负责人、期限、优先级、状态及单独的本次进展；新建表单预填原条目明确字段及建议标题。未知责任人、期限留空。切换目标重新读取当前字段/版本，保留该子目标的本次说明。来源证据保持只读。
- `POST /api/review-candidates/{candidate_id}/plan` 返回已保存草稿或生成建议；页面第一条自动展开，其余按展开时生成，避免整队列同时推理。生成失败可重试或手工补表单。
- PATCH 支持 `operations[]` 保存全部草稿；`/approve` 支持 `operations[]`，`CommandExecutor.execute_review` 在单个 BEGIN IMMEDIATE 事务内应用所有子任务、写 before/after 事件并解决父复核记录。任一目标不存在、已删除或版本冲突则全部回滚；未知目标、重复更新同一个目标、非来源片段及非法字段拒绝。重复提交同一已批准方案返回 duplicate，不重复写入。
- 更新说明追加到原说明，保留原任务来源；事件来源为本条复核会议。全部结果保存在父记录 `candidate_json.review_results`，不新增表结构。旧单表单 API 保持兼容；这是 Demo 人工复核能力，原始 native run 库仍只读，不会反写 native 生命周期复核表。
- 验证：Demo 43 项 unittest 全通过，包括多更新+新建、版本冲突原子回滚、保存恢复、幂等、非法目标/来源拦截；两组 Node 前端回归通过。真实模型样例与浏览器实测宣传制作记录生成 3 张表单（2 更新+1 新建），预填、增删、目标详情均正常。测试未确认任何实际业务复核。
- 精确备份：`integration/m3_semantic_memory_20260917/backups/split_review_20260920_104307/`（改前源文件、展示库备份和部署回执）。已按原进程环境重启 Demo，PID 写回 `data/display-service.pid`，health 正常。未重启模型和其他流水线服务，未提交/推送 Git。
- 复测：`cd M6_TaskManager_Demo && .venv/bin/python -m unittest discover -s tests -q`；有 Node 的环境运行 `node tests/test_review_ui.cjs` 和 `node tests/test_split_review_ui.cjs`。

---

### 4.6 2026-09-20 变更历史显示来源会议

任务详情的“变更历史”按每条事件的 `source_meeting_id` 关联现有会议列表，同时展示会议名称、会议日期/具体时间和单独标注的修改时间。不同会议的更新不会误用任务首次创建会议；会议无具体时刻时明确显示“具体时间未记录”，无关联时不猜测。只改静态前端，未改数据库或 API，无需重启。回归命令：`node M6_TaskManager_Demo/tests/test_event_meeting_ui.cjs`。

---

### 4.7 2026-09-20 任务清单直接修改和删除

任务清单每行新增“修改”“删除”操作，复用现有人工编辑和软删除接口。修改先读取当前任务字段后打开编辑器，保存后刷新清单和事件；从详情编辑时同步刷新详情。删除使用页面内确认框（规避内置浏览器原生 confirm 阻塞），包含任务名，删除后隐藏于默认清单，原文和审计仍保留，可勾选“含软删除”查看。已删除任务不可再次修改/删除，提交中防止重复操作，失败保留编辑内容并显示提示。原始来源会议在修改时只读。仅静态前端更新，无需重启或迁移数据库。4 组 Node 前端回归和 8 项现有 API 回归通过；新增前端测试 `M6_TaskManager_Demo/tests/test_task_actions_ui.cjs`。

---

### 4.8 2026-09-20 部门待办工作台（独立入口版，已部署）

- 管理员新增“部门工作台”导航，按当前任务 `department` 原值生成稳定独立地址 `/departments/{department_id}`，当前 21 个入口。名称不同的部门不自动合并，未分配部门的任务留在管理员清单。
- 部门页面只显示本部门 `open/in_progress/blocked` 且未删除的任务，支持项目/会议/状态筛选、搜索、详情、历史、进展反馈与状态更新。完成后移出待办。隐藏全局导入、训练样本导出、人工复核、新建、删除及部门改派。
- 新增 `app/department_workbench.py`：`GET /api/departments` 为管理员目录；`/api/departments/{id}` 下提供工作台信息、tasks、meetings、任务详情、history、PATCH 处理。任务集合在 SQL 层固定按所属部门过滤；详情/历史检查归属。仅返回相关会议名称和时间等元数据，不下发会议全量领导要求。
- PATCH 只接受 status/progress_note/expected_version，状态允许 open/in_progress/blocked/completed；服务端事务重新核对部门、未删除、未结束和版本，再追加说明并写审计。冲突拒绝，不能提交 department/title 等管理员字段。管理员既有接口保持兼容；双方读写同一展示库，处理结果同步可见。
- **访问边界**：当前没有账号登录/角色鉴权，独立地址属于管理员可访问的部门视图；上述部门范围检查不是人员身份隔离。已知其他部门地址或管理员地址仍可直接访问，不能作为正式多租户权限系统交付。后续接入部门账号时须同时保护全局管理员接口。
- 48 项 Demo unittest、5 组 Node 前端回归通过；浏览器验证目录、部门页面和处理表单。逐个检查 21 个部门待办接口均只返回本部门未结束任务，跨部门详情返回 404。没有确认或修改实际业务任务。
- 精确备份：`integration/m3_semantic_memory_20260917/backups/department_workbench_20260920_115722/`（改前源码、展示库备份、receipt.json）。已沿用原环境重启 Demo 并更新 pid，未更改模型、native run、服务绑定地址或派发。
- 复测：`cd M6_TaskManager_Demo && .venv/bin/python -m unittest discover -s tests -q`；有 Node 的环境执行 `node tests/test_department_workbench_ui.cjs`。

---

### 4.9 2026-09-20 统一组织机构与部门归并（已部署）

- 依据用户提供的两张企业通讯录截图建立公司根节点“中煤科工集团信息技术有限公司”和 18 个组织条目，顺序、名称与截图一致。另将原有“公司级、工会、团支部、纪检专干”作为保留责任主体单列，不推测其所属部门。当前数据库为 1 根 + 22 个条目，所有条目包括零任务机构均有工作台。
- 归并四组别名：北京分公司→北京分公司（信息网络部）；人工智能研究院→人工智能研究院（科技发展中心）；资产财务部→资产财务部（业财数字中心）；综合办公室→综合办公室（董事会办公室）。兼容全称英文括号写法。旧入口重定向至规范入口，旧部门 API ID 同样解析至统一机构。
- 新增 `app/organization.py`、`organization_units/organization_aliases` 表；tasks 增加 organization_id/source_department，分别保存稳定组织身份和原始部门称谓。任务 INSERT/department UPDATE 共用数据库归一触发器，覆盖导入、复核、人工修改，避免简称再次生成重复工作台。未知名称不猜测映射，在目录中单列“待确认组织归属”。
- 显式迁移 `merge_existing_tasks` 事务执行且幂等：719 条任务关联组织，13 条旧简称任务改为规范名称并增加版本/13 条 ORG_MERGE 审计；任务总量仍 719、待办仍 718。既有任务说明、原文、状态、历史事件和 65 条复核记录保持原样。原 native run 和 native Department Master 未重写，展示库入口负责规范化。
- 管理员新增“组织机构”导航和 `/api/organization`，可查看公司树、兼容称谓、待办数及统一工作台；人工编辑/复核部门字段提供规范机构候选。机构名单来源为截图，保留主体明确标注来源差异。
- 验证：54 项后端测试、5 组前端回归通过；克隆库迁移及线上对比确认原事件/复核不变，外键与 integrity_check 正常。浏览器实测组织树、合并数量、旧北京分公司地址跳转成功。归并后待办：北京 28、人工智能研究院 60、资产财务部 34、综合办公室 31。
- 精确备份：`integration/m3_semantic_memory_20260917/backups/organization_20260920_154759/`，包括改前源文件、停服后完整 SQLite 备份和 receipt.json。沿用原环境重启 Demo，pid 已更新。无账号入口版的访问边界仍同 §4.8。
- 复测：`cd M6_TaskManager_Demo && .venv/bin/python -m unittest discover -s tests -q`；迁移函数 `app.organization.merge_existing_tasks(<database_path>)` 再运行应返回 tasks_linked=0、tasks_renamed=0。

---

### 4.10 2026-09-20 用户确认组织条目调整（当前目录）

按用户最新要求，将公司级、工会、团支部、纪检专干改为正式组织条目；删除无任何关联任务的公司领导、外部董事、首席科学家（首席专家）、总助级、企业顾问及对应别名。当前为公司根 + **17 个正式组织条目**，不再显示“原有责任主体”分组。四个条目的 ID、任务和工作台链接保持不变；719 条任务及全部历史/复核记录未改动。初始化名单已同步，服务重启不会重新生成已删除的五项。55 项测试及浏览器 17 项目录核对通过。备份：`integration/m3_semantic_memory_20260917/backups/organization_adjust_20260920_161149/`；含源码、调整前数据库、部署回执。复测：`cd M6_TaskManager_Demo && .venv/bin/python -m unittest discover -s tests -q`。

---

### 4.11 2026-09-21 公司内网上线

- 正式入口：`http://192.168.30.216/meeting/`。用户已明确选择公司内网访问。Windows 客户端直连 18098 超时，因此使用现有 80 端口 Nginx 增加独立 `/meeting/` 路由；原门户 `/` 仍返回 200。
- 后端 `meeting-taskmanager.service` 已 enabled/active，Restart=always，由 yty-s 用户运行，单 worker；环境从原进程迁移到 0600 的 deploy/service.env。监听保持 127.0.0.1:18098，外部经反向代理访问，未改其他服务端口或防火墙。
- Nginx 配置宿主文件 `/mnt/data/smartcore/frontend/nginx/main.conf`（容器 frontend-main 的单文件只读 bind mount），仅添加 /meeting 路由。修改时须保留 inode，先验证候选配置、备份，再原位写回、nginx -t、nginx -s reload；不要通过原子重命名导致容器继续读取旧 inode。
- 前端支持 /meeting 前缀下的 API、部门链接和下载；旧部门重定向也保留前缀。Vue 3.5.43 已本地化到 static/vendor/vue.global.prod.js，内网客户端无需访问 CDN；完整来源和 SHA256 见 deploy/release.json。
- 验证：6 组 Node 前端回归通过；Windows 客户端验证首页、健康、JS/Vue、任务与组织 API 返回 200；17 个部门页面和任务接口逐一通过，旧简称链接跳转保持 /meeting 前缀。模型服务返回 qwen3.8-27b。浏览器自动化本次连接超时，未宣称额外的浏览器渲染验收；未重跑 native 全数据集。
- 备份：`backups/lan_release_20260921_154414/`（数据库及改前首页）和 `backups/lan_proxy_20260921_155144/`（代理配置、改前静态代码及服务配置），均位于 integration/m3_semantic_memory_20260917 下。后续部署资料在 M6_TaskManager_Demo/deploy/。
- 当前仍无账号登录/角色鉴权，仅公司内网使用；此次上线未变更既有权限模型。

---

## 5. 下一步计划 / 已知问题 / 优先级

### P0 — 全量 15 场重跑（v3_10 已验证 1a/2a，待跑全量）

- v3_10 只有前 10 场（0407–0713）。全量需**另开新目录**（fresh DB，`reset_memory=True` 会再清一次 M3Mem 按 15 场重建——当前记忆库是 v3_10 的 10 场状态）。
- **重跑命令**（先探活 vLLM）：
  ```bash
  cd /home/yty-s/meeting-m2-work/integration/m3_semantic_memory_20260917/candidate
  /home/yty-s/venvs/m6-service/bin/python integration/m6_service/native_full_dataset.py \
    --inputs /home/yty-s/meeting-m2-work/integration/m1_m3_m6_20260915/frozen_m1 \
    --departments /home/yty-s/meeting-m2-work/integration/m3_semantic_memory_20260917/departments.json \
    --output /home/yty-s/meeting-m2-work/integration/m3_semantic_memory_20260917/runs/full_semantic_v3
  ```
  约 75–90 分钟；核对 0622 乌冬→乌东、红沙泉全 15 场归并、summary `complete=true`。
- **路径教训**：`--inputs`/`--departments`/`--output` 必须给**绝对路径**（相对 candidate/ 的 `../` 解析不到 integration 级目录）；构造输入子集时**目录符号链接在 Python 3.11 `Path.glob` 下不生效**，必须用真实副本。
- 跑完导出并切 Demo：
  ```bash
  cd /home/yty-s/meeting-m2-work/integration/m3_semantic_memory_20260917
  EXPORT_SOURCE=<runs/full_semantic_v3 绝对路径> \
  EXPORT_TARGET=/home/yty-s/meeting-m2-work/M6_TaskManager_Demo/data/m1_m3_m6_semantic_v3.sqlite \
  /home/yty-s/venvs/m6-service/bin/python export_semantic_demo.py
  ```
  再按 §2 使用 systemctl 重启服务，pid 文件自动更新。

### P1 — 复核运营与余项

- **1a 余项（3 条，待业务定夺）**：剩余「部门无法唯一映射:None」均为 `PROJECT_TASK` 且 department=null 的公司级措辞条目（如「加快重点项目收尾」「推进重点项目验收」）。1a 刻意只覆盖 `NON_PROJECT_WORK`；若业务确认这类措辞也算公司级指令，可把回退放宽到 PROJECT_TASK（一行条件改动 + 一条测试）。
- **复核清零（运营建议）**：`PENDING_REVIEW_DUPLICATE` 是 CREATE 防重复护栏（`input_policy.pending_restriction`：待决复核悬着，同目标+同部门+项目兼容的后续条目一律拦入复核）。v3_10 的 6 条全部命中正确，但意味着**复核不处理会链式阻塞**——建议定期在 Demo 复核队列 approve/reject，清零后后续条目下轮自动恢复正常 CREATE/UPDATE。
- 05-25 场 REVIEW 7→12 已核查：该场条目本身歧义多 + 2 条 PENDING 护栏，非回归。

### P2 — 遗留与已知问题

- **candidate 代码未提交 git**：0917 语义改造 + 0918 的 1a/2a/单测修复/导出脚本全部只在工作区；仓库根红线目录 git status 亦有历史改动。建议尽快整理提交。
- **集成 HTTP 服务未运行**（18090/18091/18093/8093/18096）：native 运行不依赖它们（直接 import 源码），但中台/前端联调需重启。
- **平台接入未做**：M3/M6 尚未接若水中台（元数据/质量/资产/问数）与顿悟工作流；顿悟 HTTP 节点缺陷单见 `dunwu_http_node_bug.md`。
- **LLM 稳定性**：.215:8000 隔夜宕机过一次，重跑前务必探活。

### 保留的操作参考文档（未删，本文引用）

- `integration/srt_config_checklist.md`：若水中台配置清单（数据源/API/元数据/质量/资产/问数）。
- `integration/dunwu_config_checklist.md`：顿悟工作流编排清单。
- `integration/dunwu_http_node_bug.md`：顿悟 HTTP 节点缺陷单（交管理员）。
- 各模块 `README.md` 与核心模块 `M*/HANDOFF.md|HANDOVER.md`（红线目录内，模块级参考）。

---

## 6. 红线约束（违反即失败）

1. **冻结原版核心源码只读**：仓库根 `M1_Extraction/src/`、`M2_SemanticConsolidator/src/`、`M3_KnowledgeGraph/`、`M6_TaskManager/` 为原版；活的语义改动**只在** `integration/m3_semantic_memory_20260917/candidate/`。
2. **九字段 Schema 不增不减**；M3/M6 自有信息放顶层字段或独立列。
3. **不静默修正**：校验失败拒绝并留档；证据坐标只由代码算；默认部门回退必须命中 Department Master 现存条目并留痕。
4. **温度 0、禁思考**；落库幂等（sha1 稳定 ID）。
5. **记忆库标签隔离**：Neo4j 为共享实例，M3Mem 数据一律 `M3Mem`/`M3MEM_` 前缀，`reset()` 只删 M3Mem 标签，绝不触碰 Entity/Chunk/MilvusKB。
6. **派发仅 test://**：E2E 全部 route 强制 `test://`，不真实派发。

---

## 7. 快速复核命令（接手先跑）

```bash
# 模型/向量/图探活
curl -s http://192.168.30.215:8000/v1/models | head -c 80; echo
curl -s -XPOST http://127.0.0.1:8000/v1/embeddings -H 'Content-Type: application/json' -d '{"input":"测试","model":"bge-m3"}' | head -c 60; echo
curl -s http://127.0.0.1:7474 | head -c 60; echo
# Demo（应指向 v3_10 库）
curl -s http://127.0.0.1:18098/health; echo
tr '\0' '\n' < /proc/$(cat /home/yty-s/meeting-m2-work/M6_TaskManager_Demo/data/display-service.pid)/environ | grep M6_DATABASE_PATH
# 语义解析器独立验证（应 PASSED）
cd /home/yty-s/meeting-m2-work/integration/m3_semantic_memory_20260917
/home/yty-s/venvs/m6-service/bin/python probe_resolver.py 2>&1 | tail -5
# M3/M6 单测（应 24 passed / 90 passed）
cd candidate/M3_KnowledgeGraph && /home/yty-s/venvs/m6-service/bin/python -m pytest tests/test_semantic_layer.py -q 2>&1 | tail -3
cd ../M6_TaskManager && /home/yty-s/venvs/m6-service/bin/python -m pytest tests/ -q 2>&1 | tail -3
# v3_10 结果速查
/home/yty-s/venvs/m6-service/bin/python -c "import json;s=json.load(open('../runs/full_semantic_v3_10/summary.json'));print(s['complete'],s['action_counts'])"
```

**Python 环境**：`/home/yty-s/venvs/m6-service`（3.11.15，含 neo4j 6.3.1 / httpx / jsonschema）跑 M3/M6 语义链路；`/home/yty-s/venv`（3.10.12）跑 M1/M2/M4。Demo 用 `M6_TaskManager_Demo/.venv`。
