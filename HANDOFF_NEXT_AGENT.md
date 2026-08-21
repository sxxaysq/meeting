# 交接文档（交下一个 agent 执行）

日期：2026-08-21　分支：`baseline-m1-m2-m3-m6-integration`（孤儿提交，自包含）
前置阅读顺序：本文 → `integration/acceptance_report.md` → `integration/dunwu_config_checklist.md` / `integration/srt_config_checklist.md`。

---

## 1. 项目一句话总览

煤矿会议 PDF → **M1** 信息抽取（九字段 items）→ **M2** 语义归并 → **M3** 知识图谱 / **M6** 任务管理；
同时 M1 输出经 **integration 集成层**（API 壳）接入两个企业平台：
顿悟智体 `/dunwu/`（工作流，Track B 编排壳）与 嘉元数基·若水 `/srt/`（数据中台 ODS 贴源层）。

## 2. 本分支包含的模块（仓库根 = meeting-m2-work/）

| 目录 | 内容 | 端口/角色 |
|---|---|---|
| `M1_Extraction/` | M1 抽取流水线（src 只读红线；唯一例外 `dify/build_workflow.py` 已含 `--tail-ingest-url` 扩展） | — |
| `M1_Extraction/out_v14/` | **最新全量 14 份会议**：`generic/`(1939 items) 与 `block/`(2102 items) 的 `.items.json` | 数据 |
| `M2_SemanticConsolidator/` | M2 保守语义归并（权威源 M2 JSON） | — |
| `M3_KnowledgeGraph/` | M3 知识图谱 | — |
| `M6_TaskManager/`、`M6_TaskManager_Demo/` | M6 任务管理 + 演示服务 | — |
| `integration/m1_staging/` | **ingest API 壳**：`:8090`，九字段 jsonschema 严格校验、422 留档、幂等 upsert（MySQL/SQLite 双方言 repository） | 8090 |
| `integration/m1_service/` | **Track B 抽取 API 壳**：`:8091`，import 调用 M1 src，不复制逻辑 | 8091 |
| `integration/no_think_proxy_8002.py` | 禁思考兜底代理（8002→192.168.30.215:8000） | 8002 |
| `integration/*.md`、`run_*.sh`、`diff_two_paths.py` | 验收报告、平台配置清单、批处理驱动器、双路 diff | 文档/工具 |

已剔除旧链路：`M1_Preprocess`、`M1_5_ProjectNormalizer`、`M2_TaskClassifier`（不在本分支）。

## 3. 环境事实（214 开发机，hostname node2）

- Python：`/home/yty/m1x_venv/bin/python`（**不要**用系统 python；无 root、无 docker/systemd，服务一律用户态 nohup）
- vLLM：`http://192.168.30.215:8000/v1`，模型名 **`Qwen/Qwen3.6-35B-A3B`**；temperature=0；Qwen3.x 必须 `LLM_ENABLE_THINKING=false`
- MySQL（正式 staging 库）：`192.168.30.216:3306`，库 `m1_staging`，账号 `m1_dev`
- 三合一平台门户：`http://192.168.30.216`（/dunwu/ /srt/ /yuxi/）
- 会议原始 PDF：`/home/yty/数据集/原始数据/`（当前 14 份）
- **本机无法直连 github.com**（pypi 通、github 超时）；GitHub 推送/拉取需经 bundle 或有外网的机器（见 §7）

## 4. 运行中的服务与启停

```bash
# staging ingest（MySQL 形态）。DSN 必须经环境变量注入：
cd integration/m1_staging
M1_STAGING_DSN='mysql://m1_dev:<密码>@192.168.30.216:3306/m1_staging' bash start_staging.sh start|stop|status
# Track B 抽取服务（脚本内已含 vLLM 环境变量与 LLM_MAX_TOKENS=65536）
cd integration/m1_service && bash start_m1_service.sh start|stop|status
curl http://127.0.0.1:8090/healthz   # 期望 "dialect":"mysql"
curl http://127.0.0.1:8091/healthz
```

若服务未起（机器重启等）：按上面两条 start 恢复；staging 的 DSN 密码见 §8（敏感）。

## 5. 数据现状

- MySQL `m1_staging`：**14 docs / 1939 items**（generic），exact_match≈98.9%；`meeting_date` 已回填；
  视图 `dwd_meeting_item_detail`（assignee 展开）、`dws_meeting_item_stat`（按会议日期聚合）可用。
- 幂等：重复 ingest 零重复行；稳定 ID `item:m1:`+sha1(doc_id#idx)。
- 本地 SQLite 历史自测库在 `integration/m1_staging/data/`（不入库、可弃）。

## 6. 已完成 / 未完成

**已完成**：任务一决策门（顿悟无 DSL 导入→Track B 为主）；任务二 staging 全链路；双路 diff PASS（唯一差异=页码 null）；
单测 13/13 + M1 回归 139/139；14 份会议入库；历史 out 目录已清理（仅留 out_v14）。

**未完成（需人类管理员，照清单执行即可）**：
1. `/srt/` 平台 UI：数据源注册→ODS/DWD/DWS 登记→质量规则→发布 `m1-items-by-doc`/`m1-items-by-project` 两个数据服务→智能问数白名单→资产目录（`integration/srt_config_checklist.md` §1-§7）；
2. `/dunwu/` 画布按节点级清单编排 Track B 五节点：start→http(:8091/m1/extract)→code(九字段复核)→http(:8090/m1/ingest)→output（`integration/dunwu_config_checklist.md`）；
3. 防火墙 `.216→.214` 放行 8090/8091/8002；
4. 顿悟模型服务是否透传 `enable_thinking=false` 未确认，未透传则指向 8002 代理；
5. 弱口令改强（见 §8）。

## 7. Git / GitHub 现状与操作

- 远程：`github` = https://github.com/sxxaysq/meeting.git；`origin` = 本地 bundle（历史遗留，勿删 `meeting-m1-extraction.bundle`）。
- 本分支为**孤儿提交**，历史不含旧代码。**214 本机推不了 GitHub**；管理员会在有外网机器推送。
  有外网机器上：`git clone https://github.com/sxxaysq/meeting.git && cd meeting && git pull /path/to/meeting-handoff-2026-08-21.bundle baseline-m1-m2-m3-m6-integration && git push github baseline-m1-m2-m3-m6-integration`
- 214 本机取分支（无需外网）：`git fetch /home/yty/m1x/meeting-handoff-2026-08-21.bundle baseline-m1-m2-m3-m6-integration:baseline-m1-m2-m3-m6-integration`

## 8. 敏感信息（内部环境；接管后建议改强并轮换）

- MySQL DSN：`mysql://m1_dev:123456@192.168.30.216:3306/m1_staging`
- 平台门户 admin 登录密码：`admin123`（**只读调研用**；平台写操作由管理员在 UI 执行，agent 不得写平台）

## 9. 红线（违反即失败）

1. 三平台只读；平台写操作产出配置清单交管理员；
2. 中台/顿悟数据**不得写回** M1/M2/M3/M6 流水线（staging 是 ODS 贴源，权威源是 M2 JSON）；
3. 九字段 Schema 不增不减（additionalProperties:false）；
4. 温度 0、禁思考、字符坐标只由代码计算；
5. `M1_Extraction/src/` 与 M2/M3/M6 代码只读（M1 唯一例外 `dify/build_workflow.py` 的向后兼容扩展）；
6. ingest 幂等，稳定 ID sha1 派生；
7. 校验失败不静默修正：422 + 原始 payload 留档。

## 10. 常用命令速查

```bash
PY=/home/yty/m1x_venv/bin/python
# 单测
cd integration/m1_staging/tests && $PY -m pytest -q          # 13
cd M1_Extraction && $PY -m pytest tests/ -q                  # 139
# 新增会议入库（PDF 放入 /home/yty/数据集/原始数据 后）：改 run_*.sh 日期映射后执行，再 ingest-file
curl -X POST http://127.0.0.1:8090/m1/ingest-file -H 'Content-Type: application/json' \
  -d '{"path":"<items.json 绝对路径>"}'
curl "http://127.0.0.1:8090/m1/stats?source_document_id=2026-08-17"
# 兜底扫描增量入库
$PY integration/m1_staging/cron_scan.py --dir M1_Extraction/out_v14/generic --once
```

## 11. 坑位备忘

- **generic 模式必须 `LLM_MAX_TOKENS=65536`**：整篇单调用输出 2w+ tokens，8192 截断→JSON 非法→items=0（退出码仍 0，看 report 的 `llm.truncations`）；block 模式 8192 足够。
- 平台登录协议：`POST /dev-api/auth/login`，header 需 `clientid: e5cd7e4891bf95d1d19206ce24a7b32e` 与 **`tenantId: 000000`**（不是 tenant-id）。
- `meeting_date` 兜底：doc id 形如 YYYY-MM-DD 才解析，非日期形态不猜测。
- 脚本/文档输出路径统一指向 `M1_Extraction/out_v14/`。
