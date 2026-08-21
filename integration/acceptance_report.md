# M1 集成验收报告（acceptance_report）

日期：2026-08-20　范围：任务一（M1 → 顿悟）+ 任务二（M1 输出 → 数据中台）
红线自查：对 `/dunwu/`、`/srt/`、`/yuxi/` **零写操作**（仅登录 + 只读 API 查询）；
无写回流水线代码；九字段不增不减；坐标全部由代码计算；未使用 docker/systemd。

---

## 1. 决策门结论（§4.1，只读调研）

**顿悟平台无 Dify DSL 导入入口 → Track B 为主，Track A 产物作为备选交付。**

调研过程（全部只读）：
1. 登录门户 `http://192.168.30.216`（admin，密码由用户提供），取 token 后仅调用只读接口；
2. 前端主 bundle 检索：无任何 "导入 / Import / DSL" 字样（`grep 导入 = 0`、`DSL = 0`）；
   qiankun 微前端只注册了 `/srt/`、`/yuxi/` 两个子应用，顿悟模块内置于主应用路由
   （`dunwu/agent/workflow`、`dunwu/agent/workflow-builder` 等）；
3. 工作流编排是**平台自研画布**，后端 `GET /dev-api/yuxi/api/workflow`（只读查询，现存 1 个草稿工作流）；
4. 节点面板类型清单（workflow-builder 前端源码）：
   `start / llm(LLM) / output(输出) / condition(条件分支) / loop(循环) /
   http(HTTP 请求，{{变量}} 插值) / docExtractor(文档提取器) / paramExtractor(参数提取器) /
   code(代码执行，沙箱 JS/Python) / iteration(迭代) / knowledge(知识检索)`。
   ——**具备 Track B 所需全部节点类型（http + code）**；无 Dify 风格 DSL/yml 概念。

## 2. 环境探测结论（§5.1）

- 主 vLLM 模型名实测：`GET http://192.168.30.215:8000/v1/models` → **`Qwen/Qwen3.6-35B-A3B`**（已用于 DSL 与服务配置）；
- MySQL：开发机（192.168.30.214）`127.0.0.1:3306` **connection refused**，本机无实例；
  中台截图所示实例在 .216 本机，开发机不可达且无凭据（未做任何爆破尝试）。
  → 按 §5.1.2 预案：**SQLite 适配层自测 + MySQL DDL 备好**，正式切换只改 `M1_STAGING_DSN` 环境变量（repository 模式隔离方言，零代码改动）。

## 3. 交付物清单（全部位于 `/home/yty/m1x/integration/`，另有 1 个 DSL 产物在 M1 目录）

| 文件 | 说明 | 状态 |
|---|---|---|
| `m1_staging/service.py` | ingest 服务（:8090，jsonschema 严格校验、422 留档、单事务 upsert） | ✅ 运行中 |
| `m1_staging/repository.py` | 仓储层：MySQL（ON DUPLICATE KEY UPDATE）/ SQLite（ON CONFLICT）双方言 | ✅ |
| `m1_staging/cron_scan.py` | out 目录兜底扫描（内容 sha1 增量，幂等 upsert） | ✅ |
| `m1_staging/schema.sql` | MySQL 8 DDL + DWD/DWS 视图（§5.2 原文）+ SQLite 自测版留档 | ✅ |
| `m1_staging/tests/` | 13 用例：422 四类 / 幂等 / 字段回环 / ingest-file / cron 增量 / meeting_date 兜底 | ✅ 全绿 |
| `m1_staging/start_staging.sh` | 用户态启停脚本（nohup，无 systemd/docker） | ✅ |
| `m1_service/app.py` | Track B 执行服务（:8091，import 调用 src 流水线不复制逻辑） | ✅ 运行中 |
| `m1_service/start_m1_service.sh` | 用户态启停脚本（含 vLLM + 禁思考环境变量） | ✅ |
| `no_think_proxy_8002.py` + `start_no_think_proxy_8002.sh` | 禁思考兜底代理（8002 → .215:8000，复用既有实现） | ✅ |
| `diff_two_paths.py` | §4.4 双路 diff 驱动器 | ✅ |
| `dunwu_config_checklist.md` | 顿悟侧配置清单（Track B 编排 + Track A 备选） | ✅ |
| `srt_config_checklist.md` | 中台侧配置清单（数据源/ODS/DWD/DWS/质量/API/问数/资产） | ✅ |
| `diff_report.md` | 双路 diff 验收 | ✅ PASS |
| `../meeting-m2-work/M1_Extraction/dify/m1_workflow_dunwu.yml` | 顿悟版 DSL（核对后模型名 + 尾 ingest HTTP 节点） | ✅ |

唯一既有文件修改（§6 允许项）：`M1_Extraction/dify/build_workflow.py` 新增可选参数
`--tail-ingest-url`。向后兼容验证：不带该参数生成的 DSL 与既有 `m1_workflow.yml`
**语义完全一致**（YAML 结构化 diff 为空；字节级差异仅序列化空白）；`pytest tests/` 139 全绿。

## 4. 验收结果汇总

### 任务一（§4.4）
1. 双路 diff：**PASS**——160/160 items，唯一差异 = `evidence.page_start/page_end`（平台等价路恒 null），详见 `diff_report.md`；
2. `tests/test_dify_nodes.py` 与 M1 全部 pytest：**139 passed**；
3. Track B 服务端到端实测：`POST :8091/m1/extract`（200，恰九字段、真实坐标）→ `POST :8090/m1/ingest` → 可查。

### 任务二（§5.4）
1. 幂等：同一 items.json 二次 ingest 行数不变（实测 159 → 159；单测覆盖 `updated_at` 刷新）；
2. 一致：`item_json` 列回环 == 原始九字段 item（单测 `test_ingest_field_roundtrip`）；
3. 端到端时延：`/m1/ingest-file` 触发到可查 **0.094s**（要求 ≤60s）；真实批次 `cron_scan` 10 文件 159+ 条入库正常；
4. 单元测试：422 校验失败 / upsert 幂等 / cron 增量三类 **13/13 全绿**
   （`cd integration/m1_staging/tests && /home/yty/m1x_venv/bin/python -m pytest -q`）。

## 5. 阻塞项（需人类管理员处理）

| # | 阻塞项 | 替代路径（已落实） |
|---|---|---|
| 1 | ~~**MySQL 实例/凭据缺失**~~ **已解除（2026-08-20）**：管理员分配 `192.168.30.216:3306/m1_staging`（m1_dev），服务已切 MySQL，全量重灌 + 幂等复验通过 | SQLite 自测形态（历史） |
| 2 | **顿悟无 DSL 导入入口**：Track A 无法在平台真实运行 | Track B 编排清单已给到节点级（dunwu_config_checklist.md §1）；Track A DSL 留作 Dify 原生实例备选 |
| 3 | 平台服务器(.216) → 开发机(.214) 8090/8091/8002 防火墙未确认 | 管理员放行后按清单 §3 验证 |
| 4 | 顿悟侧模型服务是否透传 `enable_thinking=false` 未确认 | 已备禁思考代理 8002（复用 qwen35_9b_api 范式） |

## 6. 运行状态与复验命令

当前开发机常驻（用户态 nohup）：`:8090` staging-ingest、`:8091` m1-service。

```bash
curl http://127.0.0.1:8090/healthz          # staging
curl http://127.0.0.1:8091/healthz          # m1-service
curl "http://127.0.0.1:8090/m1/stats?source_document_id=2026-04-07"
cd /home/yty/m1x/integration/m1_staging/tests && /home/yty/m1x_venv/bin/python -m pytest -q
cd /home/yty/m1x/meeting-m2-work/M1_Extraction && /home/yty/m1x_venv/bin/python -m pytest tests/ -q
```

staging 现有数据：`out_v14/generic` 全量 14 份会议（已存 MySQL `m1_staging` 库）+ Track B 闭环样例 1 份（SQLite 历史库），
均为 ODS 贴源资产，权威源仍为 M2 JSON。

**2026-08-20 补充修复**：ingest 未显式传会议日期时，`source_document_id` 形如 YYYY-MM-DD
则确定性解析为 `meeting_date`（`repository.meeting_date_from_doc_id`，非日期形态不猜测）；
修复后 DWS 视图可按会议日期聚合，智能问数“某日会议任务数”可答。

**2026-08-20 增量入库**：数据集扩充至 14 份会议（新增 07-20/07-27/08-10/08-17），
以 generic + `LLM_MAX_TOKENS=65536` 抽取（驱动器 `integration/run_extract_new4.sh`，
产物已归并至 `M1_Extraction/out_v14/generic`）并入库；库内现 **14 docs / 1939 items**，DWS 覆盖 14 个会议日期。
（坑：generic 整篇单调用输出可达 2w+ tokens，`LLM_MAX_TOKENS=8192` 会截断致 JSON 非法→items=0。）
