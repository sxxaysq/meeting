-- ============================================================================
-- M1 staging DDL —— 嘉元数基·若水 数据中台 ODS 贴源层
-- ============================================================================
-- 权威源声明：本层为 M1 输出的 ODS 贴源资产，权威源是 M2 JSON；
-- 中台侧任何加工不得写回 M1/M2/M3/M6 流水线。
-- 两份 DDL：
--   § A. MySQL 8（正式形态，中台数据源注册用；DWD/DWS 用 JSON_TABLE 视图）
--   § B. SQLite（本机自测形态，repository.py 的 SqliteRepository 自动建表，
--         此处留档供人工核对；视图用 json_each 等价实现）
-- ============================================================================

-- ============================================================
-- § A. MySQL 8 版（库名建议：m1_staging，字符集 utf8mb4）
-- ============================================================

CREATE TABLE ods_m1_source_documents (
  source_document_id VARCHAR(128) PRIMARY KEY,
  file_name   VARCHAR(255) NOT NULL,
  meeting_date DATE NULL,
  mode        VARCHAR(16)  NOT NULL DEFAULT 'generic',
  item_count  INT NOT NULL DEFAULT 0,
  ingested_at DATETIME NOT NULL,
  updated_at  DATETIME NOT NULL,
  UNIQUE KEY uk_file (file_name)
);

CREATE TABLE ods_m1_meeting_items (
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
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uk_doc_seq (source_document_id, item_seq),
  KEY idx_project (project), KEY idx_dept (department), KEY idx_type (item_type)
);

-- DWD：责任人展开 + 文档日期
CREATE OR REPLACE VIEW dwd_meeting_item_detail AS
SELECT i.item_id, i.source_document_id, d.meeting_date, i.department, i.work_section,
       i.delivery_group, i.project, i.item_type, i.title, i.content, a.assignee,
       i.evidence_text, i.evidence_start_char, i.evidence_end_char, i.exact_match
FROM ods_m1_meeting_items i
LEFT JOIN ods_m1_source_documents d USING (source_document_id)
LEFT JOIN JSON_TABLE(i.assignees_json, '$[*]'
          COLUMNS (assignee VARCHAR(64) PATH '$')) a ON TRUE;

-- DWS：统计
CREATE OR REPLACE VIEW dws_meeting_item_stat AS
SELECT meeting_date, department, project, item_type,
       COUNT(*) AS item_cnt, SUM(exact_match) AS exact_cnt
FROM dwd_meeting_item_detail
GROUP BY meeting_date, department, project, item_type;

-- 数据质量规则建议（在中台"数据质量"模块配置，不在此执行）：
--   1) item_id 非空；
--   2) evidence_start_char <= evidence_end_char；
--   3) item_type ∈ {PROJECT_TASK, RESEARCH_TASK, NON_PROJECT_WORK, NON_TASK_ITEM}；
--   4) exact_match=1 占比 ≥ 阈值（低于即预警，不静默修）。

-- ============================================================
-- § B. SQLite 自测版（repository.py 自动建同构表，此处留档）
-- ============================================================
-- 差异说明：
--   * JSON 类型用 TEXT 存（内容仍是合法 JSON 字符串）；
--   * upsert 用 INSERT ... ON CONFLICT DO UPDATE（语义等同 ON DUPLICATE KEY UPDATE）；
--   * 无 JSON_TABLE，DWD 视图改用 json_each。

-- CREATE TABLE ods_m1_source_documents (
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
-- CREATE TABLE ods_m1_meeting_items (
--   item_id            TEXT PRIMARY KEY,          -- 'item:m1:' + sha1(doc_id#idx)，48 字符
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
--   created_at TEXT NOT NULL,
--   updated_at TEXT NOT NULL,
--   UNIQUE (source_document_id, item_seq)
-- );
-- CREATE INDEX idx_m1_project ON ods_m1_meeting_items (project);
-- CREATE INDEX idx_m1_dept    ON ods_m1_meeting_items (department);
-- CREATE INDEX idx_m1_type    ON ods_m1_meeting_items (item_type);
--
-- DROP VIEW IF EXISTS dwd_meeting_item_detail;
-- CREATE VIEW dwd_meeting_item_detail AS
-- SELECT i.item_id, i.source_document_id, d.meeting_date, i.department, i.work_section,
--        i.delivery_group, i.project, i.item_type, i.title, i.content, a.value AS assignee,
--        i.evidence_text, i.evidence_start_char, i.evidence_end_char, i.exact_match
-- FROM ods_m1_meeting_items i
-- LEFT JOIN ods_m1_source_documents d ON d.source_document_id = i.source_document_id
-- LEFT JOIN json_each(i.assignees_json) a ON 1 = 1;
--
-- DROP VIEW IF EXISTS dws_meeting_item_stat;
-- CREATE VIEW dws_meeting_item_stat AS
-- SELECT meeting_date, department, project, item_type,
--        COUNT(*) AS item_cnt, SUM(exact_match) AS exact_cnt
-- FROM dwd_meeting_item_detail
-- GROUP BY meeting_date, department, project, item_type;
