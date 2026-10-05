-- ============================================================================
-- M4 bidding DDL —— m4 招投标分离组件专用 ODS 表
-- ============================================================================
-- 建表要求与 m1_staging/schema.sql（ODS 贴源层）保持一致：
--   * ods_m4_bidding_documents 与 ods_m1_source_documents 同构（镜像，m4 自包含）；
--   * ods_m4_bidding_items 与 ods_m1_meeting_items 全部 18 列同构，
--     负责人（assignees_json，DWD 视图展开）、项目（project 列+索引）、
--     部门（department 列+索引）的关联方式与 m1 完全一致，仅追加 3 列招投标注记；
--   * item_id = "item:m4:" + sha1(source_document_id#idx)，稳定幂等；
--     origin_item_id = "item:m1:" + sha1(...) 回链 m1 原条目。
-- 两份 DDL：
--   § A. MySQL 8（正式形态，建在 m1_staging 库；DWD/DWS 用 JSON_TABLE 视图）
--   § B. SQLite（本机自测形态，repository.py 自动建表，此处留档）
-- ============================================================================

-- ============================================================
-- § A. MySQL 8 版
-- ============================================================

CREATE TABLE ods_m4_bidding_documents (
  source_document_id VARCHAR(128) PRIMARY KEY,
  file_name   VARCHAR(255) NOT NULL,
  meeting_date DATE NULL,
  mode        VARCHAR(16)  NOT NULL DEFAULT 'generic',
  item_count  INT NOT NULL DEFAULT 0,
  ingested_at DATETIME NOT NULL,
  updated_at  DATETIME NOT NULL,
  UNIQUE KEY uk_file (file_name)
);

CREATE TABLE ods_m4_bidding_items (
  item_id            CHAR(48) PRIMARY KEY,
  source_document_id VARCHAR(128) NOT NULL,
  item_seq           INT NOT NULL,
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
  item_json          JSON NOT NULL,
  bidding_category   VARCHAR(32) NOT NULL,
  bidding_reason     VARCHAR(512) NOT NULL,
  origin_item_id     CHAR(48) NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uk_doc_seq (source_document_id, item_seq),
  KEY idx_project (project), KEY idx_dept (department), KEY idx_category (bidding_category),
  KEY idx_origin (origin_item_id)
);

-- DWD：责任人展开 + 文档日期（与 dwd_meeting_item_detail 同法）
CREATE OR REPLACE VIEW dwd_m4_bidding_item_detail AS
SELECT i.item_id, i.origin_item_id, i.source_document_id, d.meeting_date,
       i.department, i.work_section, i.delivery_group, i.project, i.item_type,
       i.bidding_category, i.bidding_reason, i.title, i.content, a.assignee,
       i.evidence_text, i.evidence_start_char, i.evidence_end_char, i.exact_match
FROM ods_m4_bidding_items i
LEFT JOIN ods_m4_bidding_documents d USING (source_document_id)
LEFT JOIN JSON_TABLE(i.assignees_json, '$[*]'
          COLUMNS (assignee VARCHAR(64) PATH '$')) a ON TRUE;

-- DWS：按会议日期 / 部门 / 项目 / 招投标类目聚合
CREATE OR REPLACE VIEW dws_m4_bidding_item_stat AS
SELECT meeting_date, department, project, bidding_category,
       COUNT(*) AS item_cnt, SUM(exact_match) AS exact_cnt
FROM dwd_m4_bidding_item_detail
GROUP BY meeting_date, department, project, bidding_category;

-- 数据质量规则建议（在中台"数据质量"模块配置，本次不接中台，不在此执行）：
--   1) item_id / origin_item_id 非空；
--   2) bidding_category ∈ {TENDER, BID, OPEN_EVALUATION, AWARD_CONTRACT}；
--   3) evidence_start_char <= evidence_end_char；
--   4) exact_match=1 占比 ≥ 阈值（低于即预警，不静默修）。

-- ============================================================
-- § B. SQLite 自测版（repository.py 自动建同构表，此处留档）
-- ============================================================
-- 差异说明：
--   * JSON 类型用 TEXT 存（内容仍是合法 JSON 字符串）；
--   * upsert 用 INSERT ... ON CONFLICT DO UPDATE（语义等同 ON DUPLICATE KEY UPDATE）；
--   * 无 JSON_TABLE，DWD 视图改用 json_each。

-- CREATE TABLE ods_m4_bidding_documents (
--   source_document_id TEXT PRIMARY KEY,
--   file_name   TEXT NOT NULL,
--   meeting_date TEXT NULL,
--   mode        TEXT NOT NULL DEFAULT 'generic',
--   item_count  INTEGER NOT NULL DEFAULT 0,
--   ingested_at TEXT NOT NULL,
--   updated_at  TEXT NOT NULL,
--   UNIQUE (file_name)
-- );
--
-- CREATE TABLE ods_m4_bidding_items (
--   item_id            TEXT PRIMARY KEY,
--   source_document_id TEXT NOT NULL,
--   item_seq           INTEGER NOT NULL,
--   department         TEXT NULL,
--   work_section       TEXT NULL,
--   delivery_group     TEXT NULL,
--   project            TEXT NULL,
--   item_type          TEXT NOT NULL,
--   assignees_json     TEXT NOT NULL,
--   title              TEXT NOT NULL,
--   content            TEXT NOT NULL,
--   evidence_text      TEXT NOT NULL,
--   evidence_start_char INTEGER NOT NULL,
--   evidence_end_char   INTEGER NOT NULL,
--   evidence_page_start INTEGER NULL,
--   evidence_page_end   INTEGER NULL,
--   exact_match        INTEGER NOT NULL,
--   item_json          TEXT NOT NULL,
--   bidding_category   TEXT NOT NULL,
--   bidding_reason     TEXT NOT NULL,
--   origin_item_id     TEXT NOT NULL,
--   created_at TEXT NOT NULL,
--   updated_at TEXT NOT NULL,
--   UNIQUE (source_document_id, item_seq)
-- );
