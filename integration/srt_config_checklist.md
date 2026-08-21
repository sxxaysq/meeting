# 嘉元数基·若水（/srt/）数据中台配置清单（交人类管理员执行）

> 红线提醒：开发侧对中台**全程只读**，本清单所有注册/发布/挂载操作由管理员执行。
> 权威源原则：staging 层是 M1 输出的 **ODS 贴源资产**，权威源是 M2 JSON；
> 中台任何数据**不得写回** M1/M2/M3/M6 流水线。

## 0. 前置事实（2026-08-20 探测结论）

> **2026-08-20 进展更新（已解除）**：管理员已分配实例 `192.168.30.216:3306/m1_staging`（账号 `m1_dev`），
> §A 两表两视图已建（结构与本文件核对一致）；ingest 服务已切 MySQL 形态
> （`/healthz` 显示 `"dialect":"mysql"`）；同日增量至 **14 份会议 / 1939 items**（新增 07-20/07-27/08-10/08-17），
> 二次重灌零重复（幂等复验通过）；`meeting_date` 已回填，DWS 可按会议日期聚合。
> 下文“阻塞项”表述保留为历史快照。

- 开发机（192.168.30.214）本机 `127.0.0.1:3306` **无 MySQL 监听**（探测：connection refused）。
  中台"数据库管理"截图所示 `127.0.0.1:3306` 实例位于中台服务器（192.168.30.216）本机，
  开发机无法直连；且开发侧无凭据、不做爆破。
- **阻塞项**：需要管理员分配一个开发机可写的 MySQL 实例（中台服务器 3306 上的新库
  `m1_staging` 或另一可达实例），并提供 host/port/用户名/密码。
- 在拿到实例前，ingest 服务以 **SQLite 自测形态**运行（DSN 缺省即
  `sqlite:///integration/m1_staging/data/m1_staging.db`），DDL 两版已备好
  （`m1_staging/schema.sql`：§A MySQL 8 正式版 + §B SQLite 自测版）。
- 拿到 MySQL 后的切换方式（零代码改动，repository 模式已隔离方言）：
  `export M1_STAGING_DSN='mysql://<user>:<pass>@<host>:3306/m1_staging'`
  并重启 `bash m1_staging/start_staging.sh start`；先在库内执行 schema.sql §A 建表建视图。

## 1. 数据库管理：注册 staging 库

1. 数据接入 → 数据库管理 → 新增数据源：
   - 类型：MySQL；主机/端口：`<分配的实例>`；库名：`m1_staging`；字符集 utf8mb4；
   - 用开发账号（需 INSERT/UPDATE/SELECT 权限）；
2. 点击"测试连接"通过后保存。

## 2. ODS 贴源层登记

- staging 两张表已带 `ods_` 前缀（`ods_m1_source_documents`、`ods_m1_meeting_items`），
  直接在"数据开发 → 数据表"按 ODS 层登记；
- 如需平台内副本再挂 Seatunnel 同步（可选，非必需——数据服务可直接指向 staging 库）。

## 3. 数据开发：启用 DWD/DWS 视图

- 执行 `m1_staging/schema.sql` §A 中两条 `CREATE OR REPLACE VIEW`：
  - `dwd_meeting_item_detail`（责任人展开 + 文档日期，JSON_TABLE）；
  - `dws_meeting_item_stat`（按 meeting_date/department/project/item_type 统计）；
- 在"数据表"按 DWD / DWS 层分别登记。

## 4. 数据质量规则（数据治理 → 数据质量）

| 规则 | 说明 |
|---|---|
| `item_id` 非空 | 主键完整性 |
| `evidence_start_char <= evidence_end_char` | 坐标合法性 |
| `item_type ∈ {PROJECT_TASK, RESEARCH_TASK, NON_PROJECT_WORK, NON_TASK_ITEM}` | 四值枚举 |
| `exact_match=1` 占比 ≥ 阈值（建议 0.8） | 低于阈值触发预警，**不静默修正** |

## 5. 数据服务：发布 2 个 API（SQL 配置式）

| API 名 | SQL（参数用平台占位符语法） |
|---|---|
| `m1-items-by-doc` | `SELECT item_json FROM ods_m1_meeting_items WHERE source_document_id = #{source_document_id} ORDER BY item_seq` |
| `m1-items-by-project` | `SELECT item_json FROM ods_m1_meeting_items WHERE project = #{project} ORDER BY source_document_id, item_seq` |

发布后形如 `http://<中台>:8082/data-service/api/m1-items-by-doc?source_document_id=...`。

## 6. 智能问数

- 白名单加入 `dwd_meeting_item_detail`、`dws_meeting_item_stat`，并开启 `qa_enabled`。

## 7. 资产目录

- 挂载 `ods_m1_source_documents`、`ods_m1_meeting_items`、两张视图；
- 描述统一标注："ODS 贴源资产，权威源为 M2 JSON，禁止写回流水线"；
- `evidence_text` / `item_json` 含会议原文，安全等级按内部数据定级（建议不低于"内部"）。

## 8. 验证方法（管理员执行后）

1. 开发机触发：`curl -X POST http://192.168.30.214:8090/m1/ingest-file -H 'Content-Type: application/json' -d '{"path":"/home/yty/m1x/meeting-m2-work/M1_Extraction/out_v14/generic/2026-04-13.items.json"}'`
2. 中台侧 `m1-items-by-doc?source_document_id=2026-04-13` 应返回该批 items；
3. 智能问数抽样问题："2026-04-13 会议各项目任务数"应可由 `dws_meeting_item_stat` 回答。
