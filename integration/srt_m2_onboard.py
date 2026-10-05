#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嘉元数基·若水（/srt/）M2 接入脚本 —— 幂等、可重复执行。

平台写操作全部走**平台自己的 REST API**（与 UI 同一套），不直接改 srt_cloud 元数据库：
直连改库会漏掉平台的联动写入（列信息、缓存、版本号），后患比收益大。

用法::

    python srt_m2_onboard.py api          # 建/更新 3 个数据服务 API 并发布、自测
    python srt_m2_onboard.py metadata     # 元数据采集任务 + 修正视图节点名
    python srt_m2_onboard.py quality --run  # 6 条质量规则（--run 登记后上线并试跑）
    python srt_m2_onboard.py assets       # 9 个资产挂到「会议数据」目录（含真实挂载行）
    python srt_m2_onboard.py qa           # 智能问数白名单（4 张 DWD/DWS 视图）
    python srt_m2_onboard.py all --run    # 依次执行上述全部
    python srt_m2_onboard.py api --dry    # 只打印将要提交的 payload
    python srt_m2_onboard.py verify       # 只跑自测，不写平台

鉴权（实测结论，2026-09-03）：
- 登录：POST :8082/sys/auth/login，body {key, captcha, username, password}，
  验证码走 srt_login.py 的 OCR；
- 网关只认 **裸 token**（`Authorization: <token>`），加了 `Bearer ` 前缀会 401；
- 所有业务接口还必须带 `X-Srt-Project-Id`，否则报「缺少项目标识」；
- 分页参数名是 `page` / `limit`（不是 pageNo/pageSize）。

发布坑位（实测）：`POST /data-service/api-config` 建出来的 API 即使 payload 里
给了 `status: 1`，`release_time` 仍为 NULL，运行时 `GET :8086/api/<path>` **404**。
必须再调一次 `PUT /data-service/api-config/{id}/online` 才真正发布。
（这个路径在前端 JS 里是拼出来的，按字面量 grep 不到，靠探测 404/405 定位。）

元数据采集坑位（实测）：采集器对**视图**把 JDBC 的表类型字面量 `VIEW` 写进了
节点的 `name`（真实视图名只落在 `code` 里），于是五个视图在 UI 上全叫 VIEW、
无法区分。本脚本采集完后会把 `name` 纠正为 `code`（平台会自动重建 path）。
基表不受影响，列节点数与 MySQL 实际列数逐个对得上。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import srt_login  # noqa: E402

GATEWAY = "http://127.0.0.1:8082"
DATA_SERVICE_RUNTIME = "http://127.0.0.1:8086"
PROJECT_ID = "10002"
DATABASE_ID = 42          # M1 Staging (会议数据贴源层) → 192.168.30.216:3306/m1_staging
SQL_DB_TYPE = 1           # 1 = MySQL（与 m1 两个 API 一致）
ROOT_GROUP_NAME = "M2业务目录"
API_GROUP_NAME = "M2语义归并API目录"
SAMPLE_DOC = "2026-04-07"

# 元数据采集：用 database_id=41（名为 m1_staging），与 M1 当年那个采集任务一致，
# 这样 M2 的表挂在已有的 m1_staging 库节点下，不会在树里多出个同名库节点。
# （数据服务 API 用的是 ID:42，两个数据源指向同一个物理库。）
METADATA_DB_ID = 41
METADATA_ROOT_ID = 1
COLLECT_TASK_NAME = "m2语义归并元数据采集"

# 9 个待采集对象与其业务描述（描述会写进元数据节点，供资产目录/智能问数取用）
METADATA_OBJECTS = {
    "ods_m2_consolidated_documents": "ODS 贴源：M2 归并文档级结果（归并前后条目数、合并率、质量门状态、输入指纹）",
    "ods_m2_meeting_items": "ODS 贴源：M2 归并后的九字段权威条目 + 7 列归并注记（权威源为 M2 JSON）",
    "ods_m2_project_entities": "ODS 贴源：本文档归并产出的项目实体快照（规范名/别名/判定依据）",
    "ods_m2_validation_issues": "ODS 贴源：M2 质量门问题清单（error/review/warning，可逐条追查）",
    "dwd_m2_item_detail": "DWD：M2 条目明细 + 责任人展开 + 会议日期",
    "dwd_m2_item_lineage": "DWD：M2↔M1 条目级血缘，可 JOIN ods_m1_meeting_items 回溯每条来源",
    "dwd_m2_project_entity": "DWD：项目实体全局视图（跨文档聚合 entity_id）",
    "dws_m2_item_stat": "DWS：按会议日期/部门/项目/条目类型聚合（与 M1 DWS 同口径可对比）",
    "dws_m2_merge_stat": "DWS：归并效果统计（归并前后条目数、合并率、质量门状态、耗时）",
}

# 资产目录：挂到 M1 当年建的「会议数据」目录下（不新建目录，保持全链路资产集中）
ASSET_CATALOG_NAME = "会议数据"
# summary 列是 varchar(100)，后缀取短版保证描述不被截断；权威源路径写进清单文档。
ASSET_SUMMARY_SUFFIX = "；权威源为 M2 JSON，禁止写回流水线"

# 智能问数白名单：只放面向消费的 DWD/DWS 视图（ODS 贴源层不开放问数）。
# databaseId 用 42（与三个数据服务 API 一致）；time_column 给 meeting_date，
# 让「某次会议」类问题能走时间维度。
QA_DATABASE_ID = 42
QA_TABLES = [
    {"tableName": "dwd_m2_item_detail", "timeColumn": "meeting_date",
     "remark": "M2 归并后条目明细（责任人展开、带会议日期与归并注记）"},
    {"tableName": "dws_m2_merge_stat", "timeColumn": "meeting_date",
     "remark": "M2 归并效果统计（归并前后条目数、合并率、质量门状态）"},
    {"tableName": "dws_m2_item_stat", "timeColumn": "meeting_date",
     "remark": "M2 条目统计（按会议日期/部门/项目/条目类型）"},
    {"tableName": "dwd_m2_item_lineage", "timeColumn": "",
     "remark": "M2↔M1 条目级血缘（回链 ods_m1_meeting_items）"},
]

# M1 的两个 API 用 sqlMaxRow=100，把 159 条的会议截断了；M2 放宽到 1000。
MAX_ROW = 1000

# --------------------------------------------------------------------------
# 质量规则
# --------------------------------------------------------------------------
# 规则库（实测 GET /data-governance/quality-rule/list）：
#   1=唯一性校验 7=长度检验 8=非空检验 11=正则表达式校验 12=数据范围校验
RULE_NOT_NULL = 8
RULE_REGEX = 11
RULE_RANGE = 12

QUALITY_CATEGORY_ID = 3     # 与 M1 四条规则同一个质量主题分类
QUALITY_ORG_ID = 1
DATA_PROV = "m1_staging（MySQL 192.168.30.216:3306/m1_staging）"
DUTY = {"dutyUser": "admin", "dutyPhone": "13800138000", "dutyEmail": "admin@jiayuan.com"}

STR = "java.lang.String"
INT = "java.lang.Integer"

# 列级规则能表达的均填实阈值/正则。两条**实测得到的平台行为**必须避开：
#
# 1) rule 12（数据范围校验）**必须同时给 rangeStart 与 rangeEnd**。
#    只给一端时后端不报错、也不拦，而是把 100% 行判为异常（实测：
#    exact_match 只给 start=0 → 2055/2055 全红；给全 [0,100000000] 的
#    evidence_start_char → 0 违例）。前端有 `ruleId==12 && (!start||!end)` 拦截，
#    走 API 绕过了前端就会踩到。
# 2) MySQL **TINYINT(1) 列被 JDBC 当成 Boolean** 交付（驱动默认 tinyInt1isBit=true），
#    值是 true/false 而不是 0/1。实测：exact_match 用正则 ^[01]$ → 全红，
#    用 ^(true|false)$ → 0 违例。所以本项目的 exact_match / merged /
#    evidence_contiguous 三个 TINYINT(1) 列一律用布尔正则，不用数值范围。
#
# 跨列比较（start<=end、merged=(source_count>1)）与聚合占比（exact_match≥0.8）
# 列级规则库表达不了，写到 note 里并附人工核对 SQL，不假装有覆盖。
BOOL_REGEX = "^(true|false)$"

QUALITY_CONFIGS = [
    {
        "name": "M2 item_id 前缀与非空校验",
        "table": "ods_m2_meeting_items",
        "pk": "item_id",
        "note": "item_id 非空且形如 item:m2:+40 位 sha1，与 m1_staging 的 "
                "derive_item_id 同法派生，保证血缘可 JOIN 回 M1",
        "columns": [
            {"sourceColumn": "item_id", "sourceColumnType": STR,
             "sourceColumnRemark": "M2 条目稳定 ID", "ruleId": RULE_NOT_NULL},
            {"sourceColumn": "item_id", "sourceColumnType": STR,
             "sourceColumnRemark": "M2 条目稳定 ID", "ruleId": RULE_REGEX,
             "regexVal": "^item:m2:[0-9a-f]{40}$"},
        ],
    },
    {
        "name": "M2 证据坐标合法性校验",
        "table": "ods_m2_meeting_items",
        "pk": "item_id",
        "note": "坐标非负且不超上限。跨列约束 start_char<=end_char 列级规则库表达不了，"
                "人工核对 SQL 见 srt_config_checklist.md 的 M2 章节",
        "columns": [
            {"sourceColumn": "evidence_start_char", "sourceColumnType": INT,
             "sourceColumnRemark": "证据起始字符位置", "ruleId": RULE_RANGE,
             "rangeStart": "0", "rangeEnd": "100000000"},
            {"sourceColumn": "evidence_end_char", "sourceColumnType": INT,
             "sourceColumnRemark": "证据结束字符位置", "ruleId": RULE_RANGE,
             "rangeStart": "0", "rangeEnd": "100000000"},
        ],
    },
    {
        "name": "M2 item_type 四值枚举校验",
        "table": "ods_m2_meeting_items",
        "pk": "item_id",
        "note": "九字段 Schema 不增不减：item_type 仅允许四值枚举",
        "columns": [
            {"sourceColumn": "item_type", "sourceColumnType": STR,
             "sourceColumnRemark": "条目类型枚举", "ruleId": RULE_REGEX,
             "regexVal": "^(PROJECT_TASK|RESEARCH_TASK|NON_PROJECT_WORK|NON_TASK_ITEM)$"},
        ],
    },
    {
        "name": "M2 归并注记自洽校验",
        "table": "ods_m2_meeting_items",
        "pk": "item_id",
        "note": "source_count>=1；merged 是 TINYINT(1)，JDBC 交付为 true/false 故用布尔正则；"
                "evidence_mode / title_source 只能取枚举值",
        "columns": [
            {"sourceColumn": "source_count", "sourceColumnType": INT,
             "sourceColumnRemark": "归并来源条目数", "ruleId": RULE_RANGE,
             "rangeStart": "1", "rangeEnd": "999"},
            {"sourceColumn": "merged", "sourceColumnType": STR,
             "sourceColumnRemark": "是否合并产物（TINYINT(1)）", "ruleId": RULE_REGEX,
             "regexVal": BOOL_REGEX},
            {"sourceColumn": "evidence_mode", "sourceColumnType": STR,
             "sourceColumnRemark": "证据生成模式", "ruleId": RULE_REGEX,
             "regexVal": "^(single|bounding_span|primary_source)$"},
            {"sourceColumn": "title_source", "sourceColumnType": STR,
             "sourceColumnRemark": "标题来源", "ruleId": RULE_REGEX,
             "regexVal": "^(source|llm|source_grounding_failed)$"},
        ],
    },
    {
        "name": "M2 质量门状态枚举校验",
        "table": "ods_m2_consolidated_documents",
        "pk": "source_document_id",
        "note": "validation_status 仅允许 PASS/REVIEW/ERROR；item_count>=1、merge_operations>=0。"
                "跨表行数一致性人工核对 SQL 见 srt_config_checklist.md 的 M2 章节",
        "columns": [
            {"sourceColumn": "validation_status", "sourceColumnType": STR,
             "sourceColumnRemark": "质量门状态", "ruleId": RULE_REGEX,
             "regexVal": "^(PASS|REVIEW|ERROR)$"},
            {"sourceColumn": "item_count", "sourceColumnType": INT,
             "sourceColumnRemark": "M2 输出条目数", "ruleId": RULE_RANGE,
             "rangeStart": "1", "rangeEnd": "100000"},
            {"sourceColumn": "merge_operations", "sourceColumnType": INT,
             "sourceColumnRemark": "合并操作数", "ruleId": RULE_RANGE,
             "rangeStart": "0", "rangeEnd": "100000"},
        ],
    },
    {
        "name": "M2 布尔标志取值校验",
        "table": "ods_m2_meeting_items",
        "pk": "item_id",
        "note": "exact_match / evidence_contiguous 都是 TINYINT(1)，JDBC 交付为 true/false，"
                "数值范围与 ^[01]$ 正则会全红（已实测）；占比≥ 0.8 属聚合口径",
        "columns": [
            {"sourceColumn": "exact_match", "sourceColumnType": STR,
             "sourceColumnRemark": "精确匹配标志（TINYINT(1)）", "ruleId": RULE_REGEX,
             "regexVal": BOOL_REGEX},
            {"sourceColumn": "evidence_contiguous", "sourceColumnType": STR,
             "sourceColumnRemark": "证据是否连续（TINYINT(1)）", "ruleId": RULE_REGEX,
             "regexVal": BOOL_REGEX},
        ],
    },
]

API_SPECS = [
    {
        "path": "m2-items-by-doc",
        "note": "按会议文档查 M2 归并后的九字段条目（权威业务出口）",
        "sql": (
            "SELECT item_json FROM ods_m2_meeting_items "
            "WHERE source_document_id = #{source_document_id} ORDER BY item_seq"
        ),
        "param": {"source_document_id": SAMPLE_DOC},
        "maxRow": MAX_ROW,
    },
    {
        "path": "m2-merge-lineage-by-doc",
        "note": "按会议文档查 M2↔M1 条目级血缘（含双方标题，可直接核对合并是否合理）",
        "sql": (
            "SELECT l.m2_item_id, l.m2_item_seq, l.origin_item_id, l.source_count, "
            "m2.title AS m2_title, m1.title AS m1_title "
            "FROM dwd_m2_item_lineage l "
            "LEFT JOIN ods_m2_meeting_items m2 ON m2.item_id = l.m2_item_id "
            "LEFT JOIN ods_m1_meeting_items m1 ON m1.item_id = l.origin_item_id "
            "WHERE l.source_document_id = #{source_document_id} "
            "ORDER BY l.m2_item_seq, l.origin_item_id"
        ),
        "param": {"source_document_id": SAMPLE_DOC},
        "maxRow": MAX_ROW,
    },
    {
        "path": "m2-stat-by-doc",
        "note": "按会议文档查归并效果统计（归并前后条目数、合并率、质量门状态）",
        "sql": (
            "SELECT * FROM dws_m2_merge_stat WHERE source_document_id = #{source_document_id}"
        ),
        "param": {"source_document_id": SAMPLE_DOC},
        "maxRow": 100,
    },
]


class SrtClient:
    """若水网关的最小客户端：裸 token + X-Srt-Project-Id。"""

    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, path: str, body=None, params=None, raw_url=None):
        url = raw_url or (GATEWAY + path)
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {
            "Authorization": self.token,       # 网关只认裸 token，加 Bearer 会 401
            "X-Srt-Project-Id": PROJECT_ID,
            "Content-Type": "application/json",
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise SystemExit(
                "HTTP {} {}\n{}".format(error.code, url, error.read().decode("utf-8", "replace")[:800])
            )
        if payload.get("code") not in (0, 200):
            raise SystemExit(
                "平台返回失败 {} {}\n{}".format(
                    url, payload.get("code"), str(payload.get("msg"))[:800]
                )
            )
        return payload.get("data")

    # ---- 数据服务 API 分组 ---------------------------------------
    def find_group(self, name: str, parent_id=None):
        for node in self.call("GET", "/data-service/api-group/") or []:
            for candidate in _flatten(node):
                if candidate.get("name") == name and (
                    parent_id is None or candidate.get("parentId") == parent_id
                ):
                    return candidate
        return None

    def ensure_group(self, name: str, parent_id: int, type_: int, order_no: int, description: str):
        existing = self.find_group(name, parent_id)
        if existing:
            print("  = 分组已存在，复用 id={} name={}".format(existing["id"], name))
            return existing["id"]
        self.call(
            "POST",
            "/data-service/api-group",
            {
                "parentId": parent_id,
                "type": type_,
                "name": name,
                "orderNo": order_no,
                "description": description,
            },
        )
        created = self.find_group(name, parent_id)
        if not created:
            raise SystemExit("分组创建后查不到：{}".format(name))
        print("  + 分组已创建 id={} name={}".format(created["id"], name))
        return created["id"]

    # ---- 数据服务 API --------------------------------------------
    def list_apis(self):
        data = self.call(
            "GET", "/data-service/api-config/page", params={"page": 1, "limit": 200}
        ) or {}
        return data.get("data") or data.get("records") or data.get("list") or []

    def get_api(self, api_id: int):
        return self.call("GET", "/data-service/api-config/{}".format(api_id))

    def online_api(self, api_id: int) -> None:
        """发布。**必须单独调**：建/改时给的 status=1 不会写 release_time，
        运行时路由也就不认这个 path，直接 404。"""
        self.call("PUT", "/data-service/api-config/{}/online".format(api_id))

    # ---- 元数据 ---------------------------------------------------
    def list_children(self, parent_id: int):
        return self.call(
            "GET",
            "/data-governance/metadata/list-child",
            params={"parentId": parent_id},
        ) or []

    def get_metadata(self, node_id: int):
        return self.call("GET", "/data-governance/metadata/{}".format(node_id))

    def update_metadata(self, payload: dict) -> None:
        self.call("PUT", "/data-governance/metadata", payload)

    def list_collect_tasks(self):
        data = self.call(
            "GET",
            "/data-governance/metadata-collect/page",
            params={"page": 1, "limit": 200},
        ) or {}
        return data.get("data") or data.get("list") or []

    def create_collect_task(self) -> int:
        self.call(
            "POST",
            "/data-governance/metadata-collect",
            {
                "name": COLLECT_TASK_NAME,
                "dbType": 1,
                "databaseId": METADATA_DB_ID,
                "tableNameArr": sorted(METADATA_OBJECTS),
                "strategy": 0,
                "taskType": 1,   # 1 = 手动（不挂 cron，归并由 m2_service 触发，不靠定时采集）
                "metadataId": METADATA_ROOT_ID,
                "description": "M2 语义归并 ODS 四表 + DWD/DWS 五视图（源库 m1_staging）",
                "orgId": 11,
            },
        )
        for row in self.list_collect_tasks():
            if row.get("name") == COLLECT_TASK_NAME:
                return row["id"]
        raise SystemExit("采集任务创建后查不到：{}".format(COLLECT_TASK_NAME))

    def hand_run_collect(self, task_id: int) -> None:
        self.call("POST", "/data-governance/metadata-collect/hand-run/{}".format(task_id))

    def list_columns(self, table_node_id: int):
        return self.call(
            "GET",
            "/data-governance/metadata/list-column",
            params={"parentId": table_node_id},
        ) or []

    # ---- 质量规则 -------------------------------------------------
    def list_quality_configs(self):
        data = self.call(
            "GET",
            "/data-governance/quality-config/page",
            params={"page": 1, "limit": 200},
        ) or {}
        return data.get("data") or data.get("list") or []

    def get_quality_config(self, config_id: int):
        return self.call("GET", "/data-governance/quality-config/{}".format(config_id))

    def hand_run_quality(self, config_id: int) -> None:
        """手动试跑。注意是 **PUT**（POST 报 Request method not supported）。"""
        self.call("PUT", "/data-governance/quality-config/hand-run/{}".format(config_id))

    def online_quality(self, config_id: int) -> None:
        """上线规则（PUT，与 api-config 的 online 同一套动作命名）。"""
        self.call("PUT", "/data-governance/quality-config/online/{}".format(config_id))

    def delete_quality_config(self, config_id: int) -> None:
        """删除规则。注意契约：**DELETE 基地址 + JSON 数组 body**。
        带 id 的路径形式（DELETE /quality-config/{id}）报 method not supported，
        用 ?ids= 报 Required request body is missing。"""
        self.call("DELETE", "/data-governance/quality-config", body=[config_id])

    def locate_db_node(self) -> dict:
        """找到元数据树里的 m1_staging 库节点（metamodelId=2 为「数据库」）。"""
        for node in self.list_children(METADATA_ROOT_ID):
            if node.get("code") == "m1_staging" and node.get("metamodelId") == 2:
                return node
        raise SystemExit(
            "在根节点 {} 下找不到 m1_staging 库节点；先跑 metadata 子命令做采集。".format(
                METADATA_ROOT_ID
            )
        )

    def table_nodes(self) -> dict:
        """code -> 元数据节点（m1_staging 库下的表/视图）。"""
        db_node = self.locate_db_node()
        return {n.get("code"): n for n in self.list_children(db_node["id"])}

    # ---- 资产目录 -------------------------------------------------
    def find_catalog(self, name: str):
        for node in self.call("GET", "/data-assets/catalog/list-tree") or []:
            for candidate in _flatten(node):
                if candidate.get("name") == name:
                    return candidate
        return None

    def list_resources(self):
        data = self.call(
            "GET", "/data-assets/resource/page", params={"page": 1, "limit": 500}
        ) or {}
        return data.get("list") or data.get("data") or []

    def create_resource(self, payload: dict) -> None:
        self.call("POST", "/data-assets/resource", payload)

    def list_mounts(self, resource_id: int):
        data = self.call(
            "GET",
            "/data-assets/resource-mount/page",
            params={"page": 1, "limit": 200, "resourceId": resource_id},
        ) or {}
        return data.get("list") or data.get("data") or []

    def create_mount(self, payload: dict) -> None:
        self.call("POST", "/data-assets/resource-mount", payload)

    # ---- 智能问数白名单 -------------------------------------------
    def list_qa_configs(self):
        return self.call("GET", "/qa/manage/table-configs") or []

    def create_qa_config(self, payload: dict) -> None:
        """只有 POST（PUT 报 method not supported），改配置得先 DELETE 再 POST。"""
        self.call("POST", "/qa/manage/table-config", payload)

    def delete_qa_config(self, config_id: int) -> None:
        self.call("DELETE", "/qa/manage/table-config/{}".format(config_id))


def _flatten(node):
    yield node
    for child in node.get("children") or []:
        yield from _flatten(child)


def build_payload(spec: dict, group_id: int) -> dict:
    """按 m1-items-by-doc 的既有配置逐项对齐（GET /api-config/7 的实测字段）。"""
    return {
        "groupId": group_id,
        "path": spec["path"],
        "type": "GET",
        "name": spec["path"],
        "note": spec["note"],
        "sqlText": spec["sql"],
        "sqlSeparator": ";\n",
        "sqlMaxRow": spec["maxRow"],
        "pageAuto": 0,
        "pageParam": "page",
        "pageSizeParam": "pageSize",
        "sqlParam": json.dumps(spec["param"], ensure_ascii=False),
        "jsonParam": "{}",
        "responseResult": '{"code":0,"msg":"success","data":[]}',
        "contentType": "application/json",
        "status": 1,
        "sqlDbType": SQL_DB_TYPE,
        "databaseId": DATABASE_ID,
        "previlege": 0,
        "openTrans": 0,
        "roleIds": [],
    }


def cmd_api(client: SrtClient, dry: bool) -> int:
    print("[1/4] 确保 API 分组")
    root_id = client.ensure_group(ROOT_GROUP_NAME, 0, 1, 4, "M2 语义归并（会议数据全链路）")
    group_id = client.ensure_group(
        API_GROUP_NAME, root_id, 2, 1, "M2 归并结果与血缘查询（数据源 ID:42 m1_staging）"
    )

    print("[2/4] 创建/更新数据服务 API")
    existing = {row["path"]: row for row in client.list_apis()}
    for spec in API_SPECS:
        payload = build_payload(spec, group_id)
        if dry:
            print("  · [dry] {} -> {}".format(spec["path"], json.dumps(payload, ensure_ascii=False)))
            continue
        current = existing.get(spec["path"])
        if current:
            payload["id"] = current["id"]
            client.call("PUT", "/data-service/api-config", payload)
            print("  ~ 已更新 id={} path={}".format(current["id"], spec["path"]))
        else:
            client.call("POST", "/data-service/api-config", payload)
            print("  + 已创建 path={}".format(spec["path"]))

    if dry:
        return 0

    print("[3/4] 发布（PUT /api-config/{id}/online）")
    for row in client.list_apis():
        if row["path"] not in {spec["path"] for spec in API_SPECS}:
            continue
        detail = client.get_api(row["id"]) or {}
        if detail.get("releaseTime"):
            print("  = 已发布，跳过 id={} path={} releaseTime={}".format(
                row["id"], row["path"], detail["releaseTime"]))
            continue
        client.online_api(row["id"])
        print("  ^ 已发布 id={} path={}".format(row["id"], row["path"]))

    print("[4/4] 自测已发布 API")
    return verify()


def cmd_metadata(client: SrtClient, dry: bool) -> int:
    """元数据登记：采集任务 + 修正采集器把视图名写成 VIEW 的缺陷。"""
    print("[1/4] 定位库节点")
    db_node = client.locate_db_node()
    print("  = 库节点 id={} name={} path={}".format(
        db_node["id"], db_node.get("name"), db_node.get("path")))

    print("[2/4] 确保采集任务")
    task_id = None
    for row in client.list_collect_tasks():
        if row.get("name") == COLLECT_TASK_NAME:
            task_id = row["id"]
            print("  = 采集任务已存在，复用 id={}".format(task_id))
            break
    if task_id is None:
        if dry:
            print("  · [dry] 将创建采集任务 {}".format(COLLECT_TASK_NAME))
        else:
            task_id = client.create_collect_task()
            print("  + 采集任务已创建 id={}".format(task_id))

    print("[3/4] 校对 9 个对象是否已采集，缺则手动跑一次")
    children = {n.get("code"): n for n in client.list_children(db_node["id"])}
    missing = [name for name in METADATA_OBJECTS if name not in children]
    if missing and not dry:
        print("  ! 缺失 {} 个，执行采集：{}".format(len(missing), ", ".join(missing)))
        client.hand_run_collect(task_id)
        time.sleep(8)   # 采集是同步的，但给平台一点时间落库
        children = {n.get("code"): n for n in client.list_children(db_node["id"])}
        missing = [name for name in METADATA_OBJECTS if name not in children]
    if missing:
        print("  ! 采集后仍缺：{}".format(", ".join(missing)))

    print("[4/4] 修正节点名与描述（采集器把视图名写成了 VIEW）")
    fixed = 0
    for name, description in METADATA_OBJECTS.items():
        node = children.get(name)
        if not node:
            print("  ! {:<32} 节点缺失".format(name))
            continue
        detail = client.get_metadata(node["id"]) or {}
        need_fix = detail.get("name") != name or (detail.get("description") or "") != description
        if need_fix and not dry:
            client.update_metadata(
                {
                    "id": detail["id"],
                    "parentId": detail["parentId"],
                    "name": name,
                    "code": detail.get("code") or name,
                    "ifLeaf": detail.get("ifLeaf", 0),
                    "metamodelId": detail.get("metamodelId"),
                    "dbType": detail.get("dbType"),
                    "datasourceId": detail.get("datasourceId"),
                    "collectTaskId": detail.get("collectTaskId"),
                    "orderNo": detail.get("orderNo", 0),
                    "description": description,
                }
            )
            fixed += 1
            print("  ~ id={:<5} name: {} -> {}".format(
                detail["id"], detail.get("name"), name))
        else:
            print("  = id={:<5} {:<32} 已正确".format(detail["id"], name))

    print("小结：9 个对象已登记，本次修正 {} 个节点".format(fixed))
    return 1 if missing else 0


def cmd_quality(client: SrtClient, dry: bool, run: bool) -> int:
    """质量规则：每个校验主题一条 quality-config，列级规则携实阈值/正则。"""
    print("[1/3] 解析元数据节点 id（表节点 + 主键列节点）")
    db_node = client.locate_db_node()
    tables = {n.get("code"): n for n in client.list_children(db_node["id"])}
    column_ids = {}
    for table_name in {spec["table"] for spec in QUALITY_CONFIGS}:
        node = tables.get(table_name)
        if not node:
            raise SystemExit("元数据里没有表节点 {}；先跑 metadata 子命令。".format(table_name))
        column_ids[table_name] = {
            c.get("name") or c.get("code"): c["id"] for c in client.list_columns(node["id"])
        }
        print("  = {:<32} 表节点 id={:<5} 列节点 {} 个".format(
            table_name, node["id"], len(column_ids[table_name])))

    print("[2/3] 创建质量规则（先删后建）")
    # 坑位（实测）：`PUT /data-governance/quality-config` 对 `columnConfig` 是
    # **追加而不是替换**——同一份配置改 N 次，列规则就变成 N 倍
    # （实测 6 次重跑后 2 条列规则变成了 12 条）。所以幂等的正确做法是
    # 先把同名的旧配置整个删掉，再 POST 建新的（POST 到空配置上，追加==正确）。
    wanted = {spec["name"] for spec in QUALITY_CONFIGS}
    for row in client.list_quality_configs():
        name = row.get("name") or ""
        if name.startswith("M2") and row.get("deleted") in (0, None):
            client.delete_quality_config(row["id"])
            print("  - 删除旧配置 id={:<4} {}{}".format(
                row["id"], name, "" if name in wanted else "（孤儿，已不在规格里）"))

    created_ids = []
    for spec in QUALITY_CONFIGS:
        # note 列是 varchar(255)，超长会在平台侧报 Data truncation；
        # 开发期就拦下来，比拿一堆已写入的配置去猜哪条失败便宜得多。
        if len(spec["note"]) > 255:
            raise SystemExit(
                "规则「{}」的 note 长 {} 字，超过平台 varchar(255)上限，请改短。".format(
                    spec["name"], len(spec["note"])
                )
            )
        table_node = tables[spec["table"]]
        cols = column_ids[spec["table"]]
        pk_column = spec["pk"]
        if pk_column not in cols:
            raise SystemExit("表 {} 里找不到主键列节点 {}".format(spec["table"], pk_column))
        payload = {
            "categoryId": QUALITY_CATEGORY_ID,
            "name": spec["name"],
            "qualityType": 1,
            "status": 0,
            "taskType": 1,          # 1 = 手动触发（不挂 cron）
            "dataProv": DATA_PROV,
            "sourceTableMetaId": table_node["id"],
            "sourceTableName": spec["table"],
            "sourceTableRemark": spec["table"],
            "sourceTablePk": str(cols[pk_column]),
            "sourceTablePkRemark": pk_column,
            "sourceTablePkType": "java.lang.Long",
            "note": spec["note"],
            "orgId": QUALITY_ORG_ID,
            "columnConfig": [dict(column) for column in spec["columns"]],
            **DUTY,
        }
        if dry:
            print("  · [dry] 将创建 {} ({} 条列规则)".format(spec["name"], len(spec["columns"])))
            continue
        client.call("POST", "/data-governance/quality-config", payload)
        new_id = None
        for row in client.list_quality_configs():
            if row.get("name") == spec["name"]:
                new_id = row["id"]
        print("  + 已创建 id={:<4} {:<30} 列规则 {} 条".format(
            new_id, spec["name"], len(spec["columns"])))
        if new_id:
            created_ids.append(new_id)

    if dry:
        return 0

    print("[3/3] 上线 + 手动试跑（验证规则真能执行，不只是登记）" if run else "[3/3] 跳过上线与试跑")
    if run:
        for config_id in created_ids:
            try:
                client.online_quality(config_id)
                client.hand_run_quality(config_id)
                print("  ^ 已上线并触发 id={}".format(config_id))
            except SystemExit as error:
                # 试跑失败不影响规则已登记的事实，但必须如实报出来，不静默吞
                print("  ! id={} 上线/试跑失败：{}".format(config_id, str(error)[:200]))
    return 0


def cmd_assets(client: SrtClient, dry: bool) -> int:
    """资产目录：为 9 个 M2 对象建资产并**真正挂载**到元数据节点。

    M1 当年只建了资产、mount_status 标了 1，但 data_assets_resource_mount 里
    并没有对应挂载行（已核实）；M2 不重复这个缺口，建完资源逐个补挂载。
    """
    print("[1/3] 定位资产目录与元数据节点")
    catalog = client.find_catalog(ASSET_CATALOG_NAME)
    if not catalog:
        raise SystemExit("找不到资产目录「{}」。".format(ASSET_CATALOG_NAME))
    nodes = client.table_nodes()
    print("  = 目录 id={} name={}；元数据可用节点 {} 个".format(
        catalog["id"], catalog["name"], len(nodes)))

    print("[2/3] 创建资产")
    resources = {row.get("code"): row for row in client.list_resources()}
    for name, description in METADATA_OBJECTS.items():
        if name in resources:
            print("  = 资产已存在，复用 id={} {}".format(resources[name]["id"], name))
            continue
        if dry:
            print("  · [dry] 将创建资产 {}".format(name))
            continue
        client.create_resource(
            {
                "catalogId": catalog["id"],
                "name": name,
                "code": name,
                "summary": (description + ASSET_SUMMARY_SUFFIX)[:100],
                "status": 1,
                "openType": 1,
                "versionNo": "v1.0",
                "dutyUser": "admin",
                "dutyPhone": "",
            }
        )
        print("  + 已创建资产 {}".format(name))
    if dry:
        return 0
    resources = {row.get("code"): row for row in client.list_resources()}

    print("[3/3] 挂载到元数据节点")
    mounted = 0
    for name in METADATA_OBJECTS:
        resource = resources.get(name)
        node = nodes.get(name)
        if not resource or not node:
            print("  ! {} 缺资产或缺元数据节点，跳过".format(name))
            continue
        existing = {m.get("mountId") for m in client.list_mounts(resource["id"])}
        if node["id"] in existing:
            print("  = id={:<4} {:<32} 已挂载".format(resource["id"], name))
            continue
        client.create_mount(
            {
                "resourceId": resource["id"],
                "mountType": 1,        # 1 = 数据库表/视图（2 = API，3 = 文件）
                "mountId": node["id"],
                "mountName": node.get("path") or name,
            }
        )
        mounted += 1
        print("  + id={:<4} {:<32} 已挂载到元数据节点 {}".format(
            resource["id"], name, node["id"]))
    print("小结：{} 个资产，本次新增挂载 {} 个".format(len(METADATA_OBJECTS), mounted))
    return 0


def cmd_qa(client: SrtClient, dry: bool) -> int:
    """智能问数白名单：只放面向消费的 DWD/DWS 视图。"""
    existing = {
        (row.get("databaseId"), row.get("tableName")): row
        for row in client.list_qa_configs()
    }
    print("智能问数白名单（databaseId={}）".format(QA_DATABASE_ID))
    changed = 0
    for spec in QA_TABLES:
        key = (QA_DATABASE_ID, spec["tableName"])
        current = existing.get(key)
        if current and (current.get("remark") or "") == spec["remark"] \
                and (current.get("timeColumn") or "") == spec["timeColumn"] \
                and current.get("qaEnabled") == 1:
            print("  = id={:<4} {:<24} 已正确".format(current["id"], spec["tableName"]))
            continue
        if dry:
            print("  · [dry] 将登记 {}".format(spec["tableName"]))
            continue
        if current:
            # 没有 PUT：改配置只能先删后建
            client.delete_qa_config(current["id"])
        client.create_qa_config(
            {
                "databaseId": QA_DATABASE_ID,
                "tableName": spec["tableName"],
                "timeColumn": spec["timeColumn"],
                "qaEnabled": 1,
                "remark": spec["remark"],
            }
        )
        changed += 1
        print("  {} {:<24} timeColumn={} remark={}".format(
            "~" if current else "+", spec["tableName"],
            spec["timeColumn"] or "(无)", spec["remark"]))
    print("小结：白名单共 {} 张视图，本次变更 {} 条".format(len(QA_TABLES), changed))
    return 0


def verify(client: SrtClient = None) -> int:
    """逐个调用运行时端点，确认 code=0 且行数符合预期。

    运行时响应结构（实测）：{code, msg, data:{ifQuery, success, errorMsg,
    columns:[...], rowData:[{列名: 值}, ...]}}，行数组的键是 **rowData**。
    """
    failures = 0
    expects = {
        "m2-items-by-doc": 156,
        "m2-merge-lineage-by-doc": 159,
        "m2-stat-by-doc": 1,
    }
    for spec in API_SPECS:
        url = "{}/api/{}?{}".format(
            DATA_SERVICE_RUNTIME,
            spec["path"],
            urllib.parse.urlencode(spec["param"]),
        )
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as error:  # noqa: BLE001
            print("  ! {} 调用失败：{}".format(spec["path"], error))
            failures += 1
            continue
        data = (body or {}).get("data") or {}
        rows = data.get("rowData") or []
        want = expects.get(spec["path"])
        good = body.get("code") == 0 and data.get("success") and (want is None or len(rows) == want)
        if not good:
            failures += 1
        print(
            "  {}{:<26} code={} success={} rows={}/期望 {} columns={} errorMsg={}".format(
                "OK " if good else "!! ", spec["path"], body.get("code"), data.get("success"),
                len(rows), want, data.get("columns"), data.get("errorMsg"),
            )
        )
        if good and rows:
            first = rows[0]
            preview = {k: str(v)[:44] for k, v in list(first.items())[:3]}
            print("       首行预览: {}".format(json.dumps(preview, ensure_ascii=False)))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="若水中台 M2 接入")
    parser.add_argument(
        "command", choices=("api", "metadata", "quality", "assets", "qa", "all", "verify")
    )
    parser.add_argument("--dry", action="store_true", help="只打印 payload，不写平台")
    parser.add_argument("--run", action="store_true", help="quality 子命令：登记后手动试跑一次")
    parser.add_argument("--token", default=None, help="复用已有 token，跳过验证码登录")
    args = parser.parse_args()

    if args.command == "verify":
        return verify()

    token = args.token or (Path("/tmp/srt_token").read_text().strip()
                           if Path("/tmp/srt_token").exists() else None)
    if not token:
        print("登录若水中台（验证码 OCR）...")
        token = srt_login.login(max_tries=8)
    if not token:
        print("登录失败", file=sys.stderr)
        return 1
    Path("/tmp/srt_token").write_text(token)
    client = SrtClient(token)

    if args.command == "api":
        return cmd_api(client, args.dry)
    if args.command == "metadata":
        return cmd_metadata(client, args.dry)
    if args.command == "quality":
        return cmd_quality(client, args.dry, args.run)
    if args.command == "assets":
        return cmd_assets(client, args.dry)
    if args.command == "qa":
        return cmd_qa(client, args.dry)

    failures = cmd_api(client, args.dry)
    for step in (
        lambda: cmd_metadata(client, args.dry),
        lambda: cmd_quality(client, args.dry, args.run),
        lambda: cmd_assets(client, args.dry),
        lambda: cmd_qa(client, args.dry),
    ):
        print()
        failures += step()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
