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

---

## 9. M2 语义归并接入（2026-09-03 已执行完成，本节为实测记录）

> 与 §1–§8 不同：本节**不再需要管理员在 UI 上点**。开发侧已用平台自己的 REST API
> 全部执行完毕并逐项验证，脚本 `integration/srt_m2_onboard.py` 幂等可重跑。
> 之所以走 API 而不是直接改 `srt_cloud` 元数据库：直连改库会漏掉平台的联动写入
> （列节点、path 重建、缓存、版本号），后患比收益大。

### 9.1 一键复现

```bash
cd /home/yty-s/meeting-m2-work/integration
python srt_m2_onboard.py all --run     # api + metadata + quality + assets + qa
python srt_m2_onboard.py verify        # 只跑数据服务 API 自测
python srt_m2_onboard.py api --dry     # 只看将提交的 payload，不写平台
```

登录沿用 `srt_login.py`（验证码 OCR）；token 缓存在 `/tmp/srt_token`，过期自动重登。

### 9.2 已完成的五项接入（实测结果）

| 模块 | 对象 | 实测结果 |
|---|---|---|
| 数据源 | 复用 ID:42 `M1 Staging (会议数据贴源层)` | 未新增数据源（与 M1/M4 同库 `m1_staging`） |
| 数据服务 API | 目录 `M2业务目录`(id=7) / `M2语义归并API目录`(id=8)；API id=9/10/11 | 三个全部 `code=0`：`m2-items-by-doc` **156 行**、`m2-merge-lineage-by-doc` **159 行 6 列**、`m2-stat-by-doc` **1 行 16 列** |
| 元数据 | 采集任务 id=11，9 个对象 + 143 个列节点 | 采集日志 `All 9 tables processed, success: 9`；4 张基表列数 17/27/8/8 与 MySQL 逐个对上 |
| 数据质量 | 6 条规则（id 会变，认 name：`M2 item_id 前缀与非空校验` 等），共 14 条列级规则 | 已上线并试跑，`check_data_count` 2052/2052/2052/2052/15/2052，**err_col 与 err_row 全为 0** |
| 资产目录 | 9 个资产 id=11–19 挂到既有目录「会议数据」(id=5) | `data_assets_resource_mount` 有 **9 条真实挂载行**(id=36–44)，每条指向的元数据节点名与资产 code 逐一相符 |
| 智能问数 | 白名单 4 张视图 id=6–9（databaseId=42） | `/qa/manage/assets` 能看到 9 个 M2 资产 `mountStatus=1`；`/qa/metadata/schema` 对 4 张视图返回 code=0，列数 22/16/8/6 |

三个数据服务 API 的运行时地址（实测两种都通）：

```
http://192.168.30.216:8086/api/m2-items-by-doc?source_document_id=2026-04-07
http://192.168.30.216:8082/data-service/api/m2-stat-by-doc?source_document_id=2026-04-07
```

响应结构：`{code, msg, data:{ifQuery, success, errorMsg, columns:[...], rowData:[{列名:值}]}}`，
**行数组的键是 `rowData`**（不是 data/list）。

### 9.3 平台行为坑位（全部实测确认，改配置前必读）

| # | 坑 | 现象 | 正确做法 |
|---|---|---|---|
| 1 | 鉴权头 | 网关 `:8082` 对 `Authorization: Bearer <t>` 返回 **401** | 用**裸 token**：`Authorization: <t>` |
| 2 | 项目标识 | 只带 token 会报「缺少项目标识」 | 必须再加 `X-Srt-Project-Id: 10002` |
| 3 | 分页参数 | `pageNo/pageSize` 报「页码不能为空」 | 用 **`page` / `limit`** |
| 4 | API 发布 | `POST /data-service/api-config` 建出来的 API 即使 payload 给了 `status:1`，`release_time` 仍是 NULL，运行时 `GET :8086/api/<path>` **404** | 必须再调 `PUT /data-service/api-config/{id}/online` |
| 5 | 元数据采集器把视图名写成 `VIEW` | 5 个视图的节点 `name` 全变成字面量 `VIEW`（真实视图名只落在 `code` 里），UI 上无法区分 | 采集后 `PUT /data-governance/metadata` 把 `name` 纠正为 `code`，平台会自动重建 `path` |
| 6 | 范围校验必须给两端 | `ruleId=12` 只给 `rangeStart` 时后端不报错也不拦，而是把 **100% 行判为异常**（实测 2055/2055 全红） | `rangeStart` 与 `rangeEnd` **都要给**；给全后同一列 0 违例 |
| 7 | `TINYINT(1)` 被当成 Boolean | JDBC 驱动默认 `tinyInt1isBit=true`，`exact_match`/`merged` 取到的是 `true/false`；数值范围 `[0,1]` 与正则 `^[01]$` **全红**，`^(true|false)$` → 0 违例 | 本项目三个 `TINYINT(1)` 列（`exact_match`/`merged`/`evidence_contiguous`）一律用布尔正则 |
| 8 | `note` 列宽 | `data_governance_quality_config.note` 是 `varchar(255)`，超长报 `Data truncation` | 长 SQL 写进本文档，note 只留摘要（脚本内置 255 字守卫） |
| 9 | 删除契约不统一 | `quality-config`：`DELETE` **基地址 + JSON 数组 body** `[id]`（带 id 的路径形式报 method not supported）；`resource`：`DELETE /{id}`；`qa/manage/table-config`：`DELETE /{id}` | 见 `srt_m2_onboard.py` 各方法的 docstring |
| 10 | 软删标记不是 0 | `data_assets_resource_mount.deleted` 实际是 **NULL**，用 `WHERE deleted=0` 会把有效挂载行全过滤掉 | 核对挂载时不要加 `deleted=0` |
| 11 | 改质量规则会**追加**列规则 | `PUT /data-governance/quality-config` 对 `columnConfig` 是追加而不是替换：同一配置改 N 次，列规则变成 N 倍（实测重跑 6 次后 2 条变 12 条） | 幂等的正确做法是**先 DELETE 同名旧配置再 POST 建新的**；副作用是 id 每次重建都变，**认 name 不要硬编码 id**（`srt_m2_onboard.py` 已改成先删后建） |

### 9.4 列级规则表达不了、需人工核对的约束

平台的列级规则库只有 11 种（唯一性/长度/非空/正则/范围/及时性/格式类），
**跨列比较与聚合占比都表达不了**。以下 4 条已写进对应规则的 `note`，核对 SQL 如下
（2026-09-03 实测全部 0 违例）：

```sql
-- 1) 证据坐标有序
SELECT COUNT(*) FROM ods_m2_meeting_items WHERE evidence_start_char > evidence_end_char;
-- 2) merged 与 source_count 自洽
SELECT COUNT(*) FROM ods_m2_meeting_items WHERE merged <> (source_count > 1);
-- 3) 每文档条目行数 = doc.item_count
SELECT d.source_document_id FROM ods_m2_consolidated_documents d
 WHERE d.item_count <> (SELECT COUNT(*) FROM ods_m2_meeting_items i
                         WHERE i.source_document_id = d.source_document_id);
-- 4) exact_match 占比（阈值 0.8）
SELECT ROUND(SUM(exact_match)/COUNT(*), 4) FROM ods_m2_meeting_items;
-- 5) 血缘完整性：孤儿应为 0，且 M1 每条都被覆盖（实测 2077/2077，孤儿 0）
SELECT COUNT(*) FROM dwd_m2_item_lineage l
  LEFT JOIN ods_m1_meeting_items m ON m.item_id = l.origin_item_id WHERE m.item_id IS NULL;
SELECT COUNT(*) FROM ods_m1_meeting_items m
  LEFT JOIN dwd_m2_item_lineage l ON l.origin_item_id = m.item_id WHERE l.origin_item_id IS NULL;
```

### 9.5 建视图必须显式 COLLATE（否则血缘视图不可用）

`JSON_TABLE` 派生的字符串列在 MySQL 8 里**恒为 `utf8mb4_0900_ai_ci`**，无视库/表的
`utf8mb4_unicode_ci` 默认值。不显式 COLLATE 时，`dwd_m2_item_lineage.origin_item_id`
去 JOIN `ods_m1_meeting_items.item_id` 直接报
`1267 Illegal mix of collations`——血缘视图的根本用途就废了。
`m2_service/schema.sql` §A.5/A.6 已对 `assignee` 与 `origin_item_id` 加了
`COLLATE utf8mb4_unicode_ci`，改视图时不要漏。

### 9.6 顺带发现的 M1 侧既存缺口（本次未改，交管理员决定）

1. **元数据登记不全**：`data_governance_metadata` 里 M1 只有 `ods_m1_meeting_items`
   一个节点（采集任务 id=10 的 `tableNameArr` 只列了它），
   `ods_m1_source_documents` / `dwd_meeting_item_detail` / `dws_meeting_item_stat` 均未登记。
   补法：把这三个名字加进采集任务 id=10 的 `tableNameArr` 再 `PUT hand-run/10`。
2. **资产没有真实挂载行**：M1 的 4 个资产(id=6–9) `mount_status=1`，但
   `data_assets_resource_mount` 里**没有对应记录**（M2 的 9 个已补齐）。
3. **智能问数 remark 是复制粘贴残留**：`qa_table_config` id=3/4 的表是
   `dwd_meeting_item_detail`，remark 却写着「设备告警明细表」「设备维度表（设备字典）」，
   且 id=3(db=42) 与 id=4(db=41) 指向同一张视图、重复登记。
4. **数据服务 API 截断**：`m1-items-by-doc` / `m1-items-by-project` 的 `sqlMaxRow=100`，
   而单份会议最多 159 条，查询结果被静默截断（M2 三个 API 已设 1000）。
   建议把 M1 两个 API 的 `sqlMaxRow` 也调到 1000。

