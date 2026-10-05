# M6 FastAPI 接入服务

2026-09-14，服务器 `/home/yty-s/meeting-m2-work/integration/m6_service`。

## 已实现

通过 `M6_TaskManager.src` 命名空间只读导入 M6，避免与 M2 的同名 `src` 模块冲突。
独立 Python 3.11 环境 `/home/yty-s/venvs/m6-service`，不改旧 Python 环境或任何平台代码。
业务库为 `data/lifecycle.sqlite`，复用核心 SQLite 事务、来源血缘、审计和幂等机制。
启动入口是 `platform_app:build_app --factory`，保留原服务路由并挂载独立测试子服务。

服务地址 `http://192.168.30.216:18096`，Swagger `/docs`，OpenAPI `/openapi.json`。

| 接口 | 用途 |
| --- | --- |
| GET /healthz | 数据库可用性、Python 版本、部门数、PASS 门控 |
| GET /m6/pending | 权威 M2 文件中尚未处理及被拦截的文档 |
| POST /m6/process | 处理指定文档，或显式提交 M2 JSON |
| POST /m6/run-pending?limit=1 | 顺序处理最多 N 份 PASS 文档；1≤N≤10 |
| GET /m6/result?source_document_id=… | 查询持久化运行结果 |
| GET /m6/export/{table}?limit=100&offset=0 | 分页导出核心表，含 total/has_more |

`table` 只允许 tasks、task_events、task_source_links、task_audit、lifecycle_reviews、
processing_records、departments、dispatch_queue。导出保留数据库中的 JSON 字符串列。
分页是每个请求的一致读，跨页批量同步应在没有处理任务时进行。

处理请求示例：

```json
{"source_document_id":"2026-04-07"}
```

默认从 `integration/m2_service/data/m2/<source_document_id>.m2.json` 读源，
也可增加 `m2_payload` 字段直接传完整 M2 对象。禁止任意路径输入和额外请求字段。
不提供关闭硬校验或条目/操作限制的 HTTP 参数。REVIEW 文档按告警范围逐条处理；
旧产物没有可用范围时保守复核，不静默放行。

## 数据与安全语义

- Schema / 来源缺失或不一致 / ERROR 拒绝：HTTP 422，原始请求留在 `data/rejected/`，响应含 receipt。
- REVIEW 按条目和操作分流；身份/别名风险及未完成的相关身份复核阻止写操作，重复风险阻止 CREATE。
- 来源不存在：404；并行处理占用或已接收来源发生内容变化：409。
- M2 接收快照以 doc id 的 SHA256 命名，保存在 `data/inputs/`。内容采用规范 JSON 比较。
- 相同已完成请求返回既有结果并标记 `idempotent_replay=true`，不再次调用模型。
- 首次执行中断后重试通过核心 processing_records 续跑；已提交条目不再次执行。
- 核心按条目事务提交，整份文档不是一个事务。未完成文档不会出现完整 result 文件。
- 同一 doc id 已处理后不能直接换内容再次执行，必须先制定显式的历史对账策略。
- 进程级文件锁串行保护生命周期处理；不得在多个主机共用网络文件系统部署本库。
- 模型温度 0、禁思考；模型失败遵循核心逻辑进入人工 REVIEW，不冒充成功更新。
- 不预置猜测的部门路由；没有权威部门时相关任务可能进入 REVIEW。
- 分派只写 dispatch_queue，不发送消息，也没有自动消费分派队列的进程。
- 服务沿用现有内部集成服务的网络模式，未新增 HTTP 身份认证；勿暴露到公网。

## 启动与验证

```bash
cd /home/yty-s/meeting-m2-work/integration/m6_service
bash start_m6_service.sh
# 后台运行（先确认 18096 未占用，不重复启动）：
nohup bash start_m6_service.sh > data/logs_startup.log 2>&1 < /dev/null &
/home/yty-s/venvs/m6-service/bin/python test_service.py
# 隔离临时库，真实模型测试；不写生产生命周期库：
/home/yty-s/venvs/m6-service/bin/python smoke_live_model.py data/live_model_smoke.json
```

部署者可设 `M6_DATA_DIR`、`M6_M2_DIR`、`M6_PYTHON`、`M6_SERVICE_HOST`、
`M6_SERVICE_PORT`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`、`LLM_TIMEOUT`。
默认端口 18096、默认模型 `qwen3.8-27b`（2026-09-14 查询推理服务 `/v1/models` 确认）。
旧交接中的 `Qwen/Qwen3.6-35B-A3B` 已返回 404。无需在文件里保存密钥。

## 当前业务前置条件与中台边界

验证结果：M6 核心 32 passed；`test_service.py` 覆盖 HTTP Schema/PASS 门控、
首次 CREATE、重复请求、核心提交后中断续跑、来源变更 409、并发 409、
分页白名单、批处理 SKIP、进程重建后的幂等重放。
真实 `qwen3.8-27b` 隔离样例验证 CREATE/APPLIED 和重复请求通过，
结果存于 `data/live_model_smoke.json`；样例数据库自动清理，未污染正式库。

2026-09-14 上线实测：15 份权威 M2 JSON 全为 REVIEW，因此 pending=0、blocked=15；
部门主表为空。未将 REVIEW 改成 PASS，未创建正式业务任务。
需先由业务复核 M2 问题，并提供权威部门主数据，再做真实数据验收。
可信部门可以通过已有 M6 CLI `init-db --database <本服务库> --departments <权威JSON>` 导入；
在无运行任务时执行，不能用测试路由作为真实路由。

本服务已提供 HTTP 查询/导出能力；**尚未创建 MySQL ods_m6 表或在若水登记资产**。
若水的数据源、数据服务、资产和质量配置只能通过前端执行。
此处运行状态替代 9 月 4 日旧 HANDOFF 中“缺 Python 3.11、无 M6 接入服务”的记录。

### 2026-09-14 中台前端接入更新

上段是初始上线状态。现已通过前端登记 API 8/9/10（任务查询、合成闭环启动、结果查询），
创建并执行结果采集任务 11，执行记录 25 正常结束，将 1 条 PASS 报告写入
`srt_cloud_dataware.ods_m6_e2e_runs`，已在贴源数据预览中核对。
业务任务/事件/审计表尚未全部镜像。生产 REVIEW 和部门缺口未绕过。

新增 `POST /m6/e2e-start`（中台请求体 raw JSON `{}`）和 `GET /m6/e2e-status`。
测试使用固定合成文档 E2E-M1-M2-M6-20260914，M1/M2 现有服务处理独立测试文档，
M6 在 `/test` 子服务的独立库执行。正式 M6 服务拒绝 E2E 前缀并在 pending 中跳过测试文档。
真实服务实测 M1 1 条→M2 1 条/PASS→M6 1 任务/1 事件，血缘与重复执行通过。
完整平台操作与证据见 integration/M6_PLATFORM_E2E_ACCEPTANCE_20260914.md。

### 全量数据集回归

`POST /m6/e2e-start` 的 body 可设为 `{"dataset":"full"}`。固定15份PDF的全量测试
使用 `data/full_dataset_20260914/`：M1 staging :18190、M2 :18193 均为隔离SQLite；
M6挂载 `/full`，独立生命周期库。M1最多2份并发预取，M2/M6仍按日期顺序。
启动独立测试进程并通过文件锁防止并行重复运行；已完成文档可从检查点恢复。
再次启动会复用本轮已冻结产物；新模型/新数据需要新运行目录，不能当作新一轮推理。

全量实测15/15：M1 2176条，M2 2168条，15份REVIEW，M6全部422拒绝且无业务写入，
执行失败0。M1/M2幂等、血缘及M6拒绝检查通过；真实M6正向任务生命周期尚未通过门控。
中台采集任务11/执行记录26成功，报告已显示在ods_m6_e2e_runs。
完整报告见 integration/FULL_DATASET_TEST_20260914.md。

### 当前：按条目、按操作限制

2026-09-14 根据用户新要求，M2在issue.detail增加item_indexes/project_names/entity_ids，
M6在统一input_policy中执行操作限制并强化Schema/来源硬校验。文档REVIEW不再直接整份拒绝。
硬错误在任何条目执行前拒绝；项目身份与别名风险条目复核；既有待复核项目身份约束跨会议延续，
匹配已知规范名称、别名和来源称谓。重复风险未排除时不自动新建；其他操作仍受原有校验器限制。
M2也阻止通过传递合并绕过UNCERTAIN。

最终全量运行：E2E-FULL-ITEM-POLICY-V2-20260914；目录 data/full_dataset_item_policy_v2_20260914。
2176条冻结M1输入→2168条M2输出：752新建、466进展、68修改、12完成、867复核、3跳过。
1298条事件和来源链接，2168条处置与审计，复核条目任务来源关联0，已知策略违规0。
核心M2 84项、M6 36项、M2接入66项及M6 HTTP检查通过。
中台采集任务11、执行记录28成功，贴源数据已显示最终统计。
完整报告：integration/ITEM_OPERATION_POLICY_FULL_REVIEW_20260914.md；逐条清单在结果目录 item_dispositions.json。
V1中间结果已标SUPERSEDED，不能用于验收。生产M6库未回灌；所有执行发生在隔离测试库，未真实派发。
