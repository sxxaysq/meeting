-- ============================================================================
-- M2 semantic consolidation DDL —— 嘉元数基·若水 数据中台 ODS 贴源层
-- ============================================================================
-- 建表要求与 m1_staging/schema.sql、m4_bidding/schema.sql 保持一致：
--   * ods_m2_meeting_items 与 ods_m1_meeting_items 的 18 列全部同构，
--     仅追加 7 列 M2 归并注记（merged / source_count / origin_item_ids_json /
--     project_entity_id / evidence_mode / evidence_contiguous / title_source）；
--   * item_id = "item:m2:" + sha1(source_document_id#output_idx)，稳定幂等；
--     origin_item_ids_json 内每个元素 = "item:m1:" + sha1(source_document_id#src_idx)，
--     与 m1_staging/repository.py:derive_item_id 同法派生，可直接 JOIN 回 ods_m1_meeting_items。
--
-- 权威源声明：本层是 M2 输出的 ODS 贴源资产，权威源是 M2 JSON
--   （integration/m2_service/data/m2/<doc>.m2.json）；中台侧任何加工不得写回流水线。
-- 输入声明：M2 的输入只 SELECT ods_m1_source_documents / ods_m1_meeting_items，
--   本组件对 ods_m1_* / ods_m4_* 零写入。
--
-- 两份 DDL：
--   § A. MySQL 8（正式形态，建在 m1_staging 库；DWD/DWS 用 JSON_TABLE 视图）
--   § B. SQLite（本机自测形态，repository.py 自动建表，此处留档）
-- ============================================================================

-- ============================================================
-- § A. MySQL 8 版（库：m1_staging，字符集 utf8mb4）
-- ============================================================
-- 幂等执行：全部 CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE VIEW，
-- 由 `python cli.py bootstrap-schema` 直接跑本文件的 §A 段落。

-- ---- A.1 文档级：一次归并一行 -------------------------------------------
CREATE TABLE IF NOT EXISTS ods_m2_consolidated_documents (
  source_document_id   VARCHAR(128) PRIMARY KEY,
  file_name            VARCHAR(255) NOT NULL,
  meeting_date         DATE NULL,
  source_mode          VARCHAR(16)  NOT NULL,
  m1_item_count        INT NOT NULL DEFAULT 0,   -- 输入（M1）条目数
  item_count           INT NOT NULL DEFAULT 0,   -- 输出（M2）条目数
  merge_operations     INT NOT NULL DEFAULT 0,   -- m1_item_count - item_count
  merged_clusters      INT NOT NULL DEFAULT 0,   -- 真正发生合并的簇数
  project_entity_count INT NOT NULL DEFAULT 0,
  validation_status    VARCHAR(8)  NOT NULL,     -- PASS / REVIEW / ERROR
  issue_count          INT NOT NULL DEFAULT 0,
  input_fingerprint    CHAR(40)    NOT NULL,     -- sha1(按 item_seq 拼接的 m1 item_json)，增量判定用
  llm_calls            INT NOT NULL DEFAULT 0,
  elapsed_ms           INT NOT NULL DEFAULT 0,
  report_json          JSON NULL,                -- M2Result.report()：stats + issue_counts + schema_errors
  ingested_at DATETIME NOT NULL,
  updated_at  DATETIME NOT NULL,
  UNIQUE KEY uk_file (file_name),
  KEY idx_m2_status (validation_status),
  KEY idx_m2_date (meeting_date)
);

-- ---- A.2 条目级：M2 归并后的权威条目 ------------------------------------
CREATE TABLE IF NOT EXISTS ods_m2_meeting_items (
  item_id            CHAR(48) PRIMARY KEY,
  source_document_id VARCHAR(128) NOT NULL,
  item_seq           INT NOT NULL,               -- M2 输出序号（0 起）
  department         VARCHAR(128) NULL,
  work_section       VARCHAR(128) NULL,
  delivery_group     VARCHAR(128) NULL,
  project            VARCHAR(255) NULL,
  item_type          VARCHAR(32)  NOT NULL,
  assignees_json     JSON NOT NULL,
  title              VARCHAR(512) NOT NULL,
  content            TEXT NOT NULL,
  evidence_text      TEXT NOT NULL,
  evidence_start_char INT NOT NULL,
  evidence_end_char   INT NOT NULL,
  evidence_page_start INT NULL,
  evidence_page_end   INT NULL,
  exact_match        TINYINT(1) NOT NULL,
  item_json          JSON NOT NULL,              -- 九字段原文，与 M1 同 Schema
  -- ↓ M2 追加的 7 列归并注记
  merged              TINYINT(1) NOT NULL,       -- source_count > 1
  source_count        INT NOT NULL,              -- 该输出条目由几条 M1 条目归并而来
  origin_item_ids_json JSON NOT NULL,            -- ["item:m1:"+sha1(doc#src_idx), ...] 回链 M1
  project_entity_id   VARCHAR(32) NULL,          -- 项目实体归一结果（catalog 生成，形如 P0001）
  evidence_mode       VARCHAR(16) NOT NULL,      -- single / bounding_span / primary_source
  evidence_contiguous TINYINT(1) NOT NULL,
  title_source        VARCHAR(32) NOT NULL,      -- source / llm / source_grounding_failed
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uk_doc_seq (source_document_id, item_seq),
  KEY idx_project (project), KEY idx_dept (department), KEY idx_type (item_type),
  KEY idx_entity (project_entity_id), KEY idx_merged (merged)
);

-- ---- A.3 项目实体级：本文档这次归并产出的实体快照 ------------------------
-- PK 带 source_document_id：同一实体跨会议会在各文档下各存一行（ODS 贴源语义），
-- 全局实体视图由 dwd_m2_project_entity 聚合得到。
CREATE TABLE IF NOT EXISTS ods_m2_project_entities (
  source_document_id VARCHAR(128) NOT NULL,
  entity_id          VARCHAR(32)  NOT NULL,
  canonical_name     VARCHAR(255) NOT NULL,
  aliases_json       JSON NOT NULL,
  source_names_json  JSON NOT NULL,
  decisions_json     JSON NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (source_document_id, entity_id),
  KEY idx_entity (entity_id), KEY idx_canonical (canonical_name)
);

-- ---- A.4 质量门问题级：REVIEW/ERROR 的可追查清单 -------------------------
-- 无跨次稳定键，落库时按 source_document_id 先删后插（同一事务）。
CREATE TABLE IF NOT EXISTS ods_m2_validation_issues (
  issue_id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  source_document_id VARCHAR(128) NOT NULL,
  code               VARCHAR(48)  NOT NULL,
  level              VARCHAR(8)   NOT NULL,      -- error / review / warning
  message            VARCHAR(1024) NOT NULL,
  item_index         INT NULL,                   -- 指向 M2 输出 item_seq；文档级问题为 NULL
  detail_json        JSON NULL,
  created_at DATETIME NOT NULL,
  KEY idx_doc (source_document_id), KEY idx_code (code), KEY idx_level (level)
);

-- ---- A.5 DWD：责任人展开 + 文档日期（与 dwd_meeting_item_detail 同法）----
-- 坑位提醒：JSON_TABLE 派生的字符串列在 MySQL 8 里**恒为 utf8mb4_0900_ai_ci**，
-- 无视库/表的 utf8mb4_unicode_ci 默认值；不显式 COLLATE 的话，拿它去 JOIN
-- 任何基表列会报 1267 Illegal mix of collations。
CREATE OR REPLACE VIEW dwd_m2_item_detail AS
SELECT i.item_id, i.source_document_id, d.meeting_date, d.source_mode,
       i.department, i.work_section, i.delivery_group, i.project, i.item_type,
       i.project_entity_id, i.title, i.content,
       a.assignee COLLATE utf8mb4_unicode_ci AS assignee,
       i.merged, i.source_count, i.evidence_mode, i.evidence_contiguous, i.title_source,
       i.evidence_text, i.evidence_start_char, i.evidence_end_char, i.exact_match
FROM ods_m2_meeting_items i
LEFT JOIN ods_m2_consolidated_documents d USING (source_document_id)
LEFT JOIN JSON_TABLE(i.assignees_json, '$[*]'
          COLUMNS (assignee VARCHAR(64) PATH '$')) a ON TRUE;

-- ---- A.6 DWD：M2 ↔ M1 血缘（一行一条来源回链，可直接 JOIN ods_m1_meeting_items）
-- origin_item_id 必须 COLLATE 成与 ods_m1_meeting_items.item_id 一致的
-- utf8mb4_unicode_ci，否则这个视图的根本用途（JOIN 回 M1）直接报 1267。
CREATE OR REPLACE VIEW dwd_m2_item_lineage AS
SELECT i.source_document_id, i.item_id AS m2_item_id, i.item_seq AS m2_item_seq,
       i.merged, i.source_count,
       l.origin_item_id COLLATE utf8mb4_unicode_ci AS origin_item_id
FROM ods_m2_meeting_items i
JOIN JSON_TABLE(i.origin_item_ids_json, '$[*]'
          COLUMNS (origin_item_id CHAR(48) PATH '$')) l ON TRUE;

-- ---- A.7 DWD：项目实体全局视图（跨文档聚合）------------------------------
CREATE OR REPLACE VIEW dwd_m2_project_entity AS
SELECT entity_id,
       MIN(canonical_name) AS canonical_name,
       COUNT(DISTINCT source_document_id) AS document_cnt,
       JSON_ARRAYAGG(canonical_name) AS canonical_name_variants
FROM ods_m2_project_entities
GROUP BY entity_id;

-- ---- A.8 DWS：条目统计（与 dws_meeting_item_stat 同法，便于 M1/M2 对比）---
-- 注意：本视图建在责任人展开后的 dwd_m2_item_detail 上，item_cnt 与 M1 的
-- dws_meeting_item_stat 一样会按 assignee 个数放大（两边同口径，可直接对比）；
-- 要"条目数"本数请读 distinct_item_cnt。
CREATE OR REPLACE VIEW dws_m2_item_stat AS
SELECT meeting_date, department, project, item_type,
       COUNT(*) AS item_cnt,
       COUNT(DISTINCT item_id) AS distinct_item_cnt,
       SUM(exact_match) AS exact_cnt, SUM(merged) AS merged_cnt
FROM dwd_m2_item_detail
GROUP BY meeting_date, department, project, item_type;

-- ---- A.9 DWS：归并效果统计 ------------------------------------------------
CREATE OR REPLACE VIEW dws_m2_merge_stat AS
SELECT source_document_id, file_name, meeting_date, source_mode,
       m1_item_count, item_count, merge_operations, merged_clusters,
       ROUND(merge_operations / NULLIF(m1_item_count, 0), 4) AS merge_rate,
       project_entity_count, validation_status, issue_count,
       llm_calls, elapsed_ms, ingested_at, updated_at
FROM ods_m2_consolidated_documents;

-- 数据质量规则建议（在中台"数据质量"模块配置，不在本文件执行）：
--   1) item_id 非空且以 'item:m2:' 开头；
--   2) evidence_start_char <= evidence_end_char；
--   3) item_type ∈ {PROJECT_TASK, RESEARCH_TASK, NON_PROJECT_WORK, NON_TASK_ITEM}；
--   4) source_count >= 1 且 merged = (source_count > 1)；
--   5) validation_status ∈ {PASS, REVIEW, ERROR}；
--   6) ods_m2_meeting_items 每文档行数 = ods_m2_consolidated_documents.item_count。

-- ============================================================
-- § B. SQLite 自测版（repository.py 自动建同构表，此处留档）
-- ============================================================
-- 差异说明：
--   * JSON 类型用 TEXT 存（内容仍是合法 JSON 字符串）；
--   * upsert 用 INSERT ... ON CONFLICT DO UPDATE（语义等同 ON DUPLICATE KEY UPDATE）；
--   * 无 JSON_TABLE，DWD 视图改用 json_each；
--   * 自测库是独立文件，因此 repository 会**同时**建 ods_m1_* 两张源表
--     （CREATE TABLE IF NOT EXISTS，仅供单测灌入 M1 输入）；
--     MySQL 形态下 ods_m1_* 由 m1_staging 建好，本组件绝不 CREATE / 写入它们。

-- CREATE TABLE IF NOT EXISTS ods_m1_source_documents (
--   source_document_id TEXT PRIMARY KEY,
--   file_name   TEXT NOT NULL,
--   meeting_date TEXT NULL,
--   mode        TEXT NOT NULL DEFAULT 'generic',
--   item_count  INTEGER NOT NULL DEFAULT 0,
--   ingested_at TEXT NOT NULL,
--   updated_at  TEXT NOT NULL,
--   UNIQUE (file_name)
-- );
-- CREATE TABLE IF NOT EXISTS ods_m1_meeting_items (
--   item_id TEXT PRIMARY KEY, source_document_id TEXT NOT NULL, item_seq INTEGER NOT NULL,
--   department TEXT NULL, work_section TEXT NULL, delivery_group TEXT NULL, project TEXT NULL,
--   item_type TEXT NOT NULL, assignees_json TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
--   evidence_text TEXT NOT NULL, evidence_start_char INTEGER NOT NULL, evidence_end_char INTEGER NOT NULL,
--   evidence_page_start INTEGER NULL, evidence_page_end INTEGER NULL, exact_match INTEGER NOT NULL,
--   item_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
--   UNIQUE (source_document_id, item_seq)
-- );
--
-- CREATE TABLE IF NOT EXISTS ods_m2_consolidated_documents (
--   source_document_id TEXT PRIMARY KEY, file_name TEXT NOT NULL, meeting_date TEXT NULL,
--   source_mode TEXT NOT NULL, m1_item_count INTEGER NOT NULL DEFAULT 0,
--   item_count INTEGER NOT NULL DEFAULT 0, merge_operations INTEGER NOT NULL DEFAULT 0,
--   merged_clusters INTEGER NOT NULL DEFAULT 0, project_entity_count INTEGER NOT NULL DEFAULT 0,
--   validation_status TEXT NOT NULL, issue_count INTEGER NOT NULL DEFAULT 0,
--   input_fingerprint TEXT NOT NULL, llm_calls INTEGER NOT NULL DEFAULT 0,
--   elapsed_ms INTEGER NOT NULL DEFAULT 0, report_json TEXT NULL,
--   ingested_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (file_name)
-- );
-- CREATE TABLE IF NOT EXISTS ods_m2_meeting_items (
--   item_id TEXT PRIMARY KEY, source_document_id TEXT NOT NULL, item_seq INTEGER NOT NULL,
--   department TEXT NULL, work_section TEXT NULL, delivery_group TEXT NULL, project TEXT NULL,
--   item_type TEXT NOT NULL, assignees_json TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
--   evidence_text TEXT NOT NULL, evidence_start_char INTEGER NOT NULL, evidence_end_char INTEGER NOT NULL,
--   evidence_page_start INTEGER NULL, evidence_page_end INTEGER NULL, exact_match INTEGER NOT NULL,
--   item_json TEXT NOT NULL,
--   merged INTEGER NOT NULL, source_count INTEGER NOT NULL, origin_item_ids_json TEXT NOT NULL,
--   project_entity_id TEXT NULL, evidence_mode TEXT NOT NULL, evidence_contiguous INTEGER NOT NULL,
--   title_source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
--   UNIQUE (source_document_id, item_seq)
-- );
-- CREATE TABLE IF NOT EXISTS ods_m2_project_entities (
--   source_document_id TEXT NOT NULL, entity_id TEXT NOT NULL, canonical_name TEXT NOT NULL,
--   aliases_json TEXT NOT NULL, source_names_json TEXT NOT NULL, decisions_json TEXT NOT NULL,
--   created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
--   PRIMARY KEY (source_document_id, entity_id)
-- );
-- CREATE TABLE IF NOT EXISTS ods_m2_validation_issues (
--   issue_id INTEGER PRIMARY KEY AUTOINCREMENT, source_document_id TEXT NOT NULL,
--   code TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL,
--   item_index INTEGER NULL, detail_json TEXT NULL, created_at TEXT NOT NULL
-- );
--
-- DROP VIEW IF EXISTS dwd_m2_item_detail;
-- CREATE VIEW dwd_m2_item_detail AS
-- SELECT i.item_id, i.source_document_id, d.meeting_date, d.source_mode,
--        i.department, i.work_section, i.delivery_group, i.project, i.item_type,
--        i.project_entity_id, i.title, i.content, a.value AS assignee,
--        i.merged, i.source_count, i.evidence_mode, i.evidence_contiguous, i.title_source,
--        i.evidence_text, i.evidence_start_char, i.evidence_end_char, i.exact_match
-- FROM ods_m2_meeting_items i
-- LEFT JOIN ods_m2_consolidated_documents d ON d.source_document_id = i.source_document_id
-- LEFT JOIN json_each(i.assignees_json) a ON 1 = 1;
--
-- DROP VIEW IF EXISTS dwd_m2_item_lineage;
-- CREATE VIEW dwd_m2_item_lineage AS
-- SELECT i.source_document_id, i.item_id AS m2_item_id, i.item_seq AS m2_item_seq,
--        i.merged, i.source_count, l.value AS origin_item_id
-- FROM ods_m2_meeting_items i JOIN json_each(i.origin_item_ids_json) l ON 1 = 1;
--
-- DROP VIEW IF EXISTS dwd_m2_project_entity;
-- CREATE VIEW dwd_m2_project_entity AS
-- SELECT entity_id, MIN(canonical_name) AS canonical_name,
--        COUNT(DISTINCT source_document_id) AS document_cnt,
--        JSON_GROUP_ARRAY(canonical_name) AS canonical_name_variants
-- FROM ods_m2_project_entities GROUP BY entity_id;
--
-- DROP VIEW IF EXISTS dws_m2_item_stat;
-- CREATE VIEW dws_m2_item_stat AS
-- SELECT meeting_date, department, project, item_type,
--        COUNT(*) AS item_cnt, COUNT(DISTINCT item_id) AS distinct_item_cnt,
--        SUM(exact_match) AS exact_cnt, SUM(merged) AS merged_cnt
-- FROM dwd_m2_item_detail GROUP BY meeting_date, department, project, item_type;
--
-- DROP VIEW IF EXISTS dws_m2_merge_stat;
-- CREATE VIEW dws_m2_merge_stat AS
-- SELECT source_document_id, file_name, meeting_date, source_mode,
--        m1_item_count, item_count, merge_operations, merged_clusters,
--        ROUND(CAST(merge_operations AS REAL) / NULLIF(m1_item_count, 0), 4) AS merge_rate,
--        project_entity_count, validation_status, issue_count,
--        llm_calls, elapsed_ms, ingested_at, updated_at
-- FROM ods_m2_consolidated_documents;
