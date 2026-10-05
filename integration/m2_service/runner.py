# -*- coding: utf-8 -*-
"""M2 归并编排层：从数据中台取 M1 输出 → 跑 M2 → 权威 JSON 落盘 → 写回 ods_m2_*。

service.py 与 cli.py 共用本模块，业务逻辑只此一份。

红线（违反即失败）：
1. `M2_SemanticConsolidator/src/` 与 `M1_Extraction/src/` **零修改**，只 sys.path + import；
2. 对 `ods_m1_*` / `ods_m4_*` **只 SELECT**，中台数据不写回 M1 流水线；
3. M2 权威输出仍是 `data/m2/<doc>.m2.json`，`ods_m2_*` 只是它的 ODS 贴源副本；
4. 九字段 Schema 不增不减；
5. 校验失败**不静默修正**：422 + 原始 payload 留档 `data/rejected/`；
6. `normalized_text` 本机不可得（无 .body.txt、历史 PDF 不在），传 None；
   合并条目 evidence 因此走 M2 既有的 `primary_source` 降级路径，
   **不会**把拼接文字伪装成 exact_match。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
M2_ROOT = REPO_ROOT / "M2_SemanticConsolidator"
M1_ROOT = REPO_ROOT / "M1_Extraction"
M2_SRC = M2_ROOT / "src"

# M2 与 M1 存在同名模块（llm_client / pipeline / schema_validator / quality_gate / cli）。
# 这里**只**把 M2 的 src 放到 sys.path 最前：M2 的 llm_client.py 是自包含副本，
# 加 M1 src 会让两边互相覆盖，解析到错的流水线。
if str(M2_SRC) not in sys.path:
    sys.path.insert(0, str(M2_SRC))
if str(HERE) not in sys.path:
    sys.path.insert(1, str(HERE))

import jsonschema  # noqa: E402

from llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402  (M2 自带副本)
from pipeline import run_pipeline  # noqa: E402
from project_normalizer import ProjectCatalog  # noqa: E402

from repository import (  # noqa: E402
    M2Repository,
    fingerprint_items,
    meeting_date_from_doc_id,
)

M1_ITEMS_SCHEMA = M1_ROOT / "schemas" / "m1_items.schema.json"
M2_OUTPUT_SCHEMA = M2_ROOT / "schemas" / "m2_output.schema.json"

# 数据目录可经 M2_DATA_DIR 改指（单测隔离用）；缺省为本组件下的 data/。
DATA_DIR = Path(os.getenv("M2_DATA_DIR") or (HERE / "data"))
M2_OUT_DIR = DATA_DIR / "m2"
REJECT_DIR = DATA_DIR / "rejected"
# 项目主表：跨文档共享的 alias / entity_id 存储，可经 M2_CATALOG_PATH 改指。
CATALOG_PATH = Path(os.getenv("M2_CATALOG_PATH") or (DATA_DIR / "project_catalog.sqlite"))

SOURCE_MODES = ("block", "generic")

logging.basicConfig(
    level=os.getenv("M2_SERVICE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("m2_service")

# 项目主表 catalog 是 SQLite 单文件，`next_entity_id()` = COUNT(*)+1，
# 并发写会派生出同一个 entity_id 撞主键；归并本身也没必要并发。
# 因此整个归并过程串行化，抢不到锁直接 409，不排队（避免 HTTP 节点超时）。
# 用 RLock：批量入口（_run_batch）与单文档（consolidate_document）都要持锁，
# 同线程重入必须放行，否则自己死锁；跨线程仍然互斥。
_RUN_LOCK = threading.RLock()


class M2Error(Exception):
    """带 HTTP 语义的业务异常，由 service.py 映射成对应状态码。"""

    def __init__(self, status_code: int, detail: str, **extra: Any):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.extra = extra


@contextmanager
def _run_lock():
    if not _RUN_LOCK.acquire(blocking=False):
        raise M2Error(409, "已有 M2 归并任务在运行，请稍后重试（项目主表非并发安全）")
    try:
        yield
    finally:
        _RUN_LOCK.release()


# --------------------------------------------------------------------------
# Schema 与留档
# --------------------------------------------------------------------------

_SCHEMA_CACHE: Dict[str, Any] = {}


def _validator(path: Path) -> jsonschema.Draft7Validator:
    key = str(path)
    if key not in _SCHEMA_CACHE:
        if not path.exists():
            raise M2Error(500, "找不到 Schema 文件：{}".format(path))
        schema = json.loads(path.read_text(encoding="utf-8"))
        _SCHEMA_CACHE[key] = jsonschema.Draft7Validator(schema)
    return _SCHEMA_CACHE[key]


def _schema_errors(path: Path, payload: Any, limit: int = 20) -> List[str]:
    errors = sorted(_validator(path).iter_errors(payload), key=lambda e: [str(p) for p in e.path])
    return [
        "{}: {}".format("/".join(str(p) for p in error.path) or "$", error.message)
        for error in errors[:limit]
    ]


def _preserve(payload: Any, label: str) -> str:
    """被拒绝的原始 payload 落盘留档，供人工排查（不进库、不修正）。"""
    REJECT_DIR.mkdir(parents=True, exist_ok=True)
    path = REJECT_DIR / "{}_{}.json".format(label, int(time.time() * 1000))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _safe_name(source_document_id: str) -> str:
    """doc id 直接当文件名用；把路径分隔符与 Windows 保留字符换掉，不做其它改写。"""
    return re.sub(r'[<>:"/\\|?*]', "_", source_document_id) or "unknown"


def _ensure_data_dirs() -> None:
    """ProjectCatalog 直接 sqlite3.connect(path)，不会建父目录；
    首次跑时 data/ 还不存在会报 unable to open database file。"""
    for path in (DATA_DIR, M2_OUT_DIR, REJECT_DIR, CATALOG_PATH.parent):
        path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# 单文档归并
# --------------------------------------------------------------------------


def llm_backend() -> dict:
    """/healthz 用：探测 LLM 配置，永不抛。"""
    try:
        config = LLMConfig.from_env()
        return {
            "base_url": config.base_url,
            "model": config.model,
            "transport": config.transport,
            "enable_thinking": config.enable_thinking,
            "temperature": config.temperature,
        }
    except Exception as error:  # noqa: BLE001 - healthz 永不 500
        return {"error": str(error)}


def _align_trace(items: List[dict], trace: List[dict]) -> List[dict]:
    """按 item_index 对齐 merge_trace 与输出 items。

    M2 保证 merge_trace 覆盖每一条输出 Item（否则它自己的 Schema 就失败了），
    但对不齐属于上游契约被破坏，直接 500 报错，不按位置猜。
    """
    by_index = {entry.get("item_index"): entry for entry in trace}
    aligned = []
    for idx in range(len(items)):
        entry = by_index.get(idx)
        if entry is None:
            raise M2Error(
                500,
                "merge_trace 未覆盖输出条目 item_index={}（M2 追溯契约被破坏）".format(idx),
            )
        aligned.append(entry)
    return aligned


def consolidate_document(
    repository: M2Repository,
    source_document_id: str,
    workers: int = 1,
    force: bool = False,
    no_llm: bool = False,
    allow_invalid: bool = False,
    catalog_path: Optional[str] = None,
) -> dict:
    """对一份会议文档跑完整 M2 链路并落库。返回扁平 summary。

    ``force=False`` 且输入指纹未变时直接跳过（skipped=True），不重跑 LLM。
    """
    document = repository.fetch_document(source_document_id)
    if document is None:
        raise M2Error(404, "数据中台没有这份文档：{}".format(source_document_id))

    items = document["items"]
    if not items:
        raise M2Error(
            400,
            "文档 {} 在 ods_m1_meeting_items 里没有条目，无可归并的输入".format(source_document_id),
        )

    source_mode = document.get("mode")
    if source_mode not in SOURCE_MODES:
        # M2 的 source_mode 必填且**不从内容猜**：猜错会把 block 的强结构约束
        # 套到 generic 输出上，两种模式的假设互相污染。
        raise M2Error(
            400,
            "ods_m1_source_documents.mode={!r} 不是 {}，拒绝归并（不从内容猜来源模式）".format(
                source_mode, "/".join(SOURCE_MODES)
            ),
        )

    fingerprint = fingerprint_items(items)
    existing = repository.stats(source_document_id).get("document")
    if not force and existing and existing.get("input_fingerprint") == fingerprint:
        logger.info("skip doc=%s（M1 输入指纹未变）", source_document_id)
        # 字段集与成功分支对齐，否则批量报表里跳过的文档会出现 None
        return {
            "status": "skipped",
            "skipped": True,
            "source_document_id": source_document_id,
            "reason": "input_fingerprint_unchanged",
            "source_mode": existing.get("source_mode"),
            "m1_item_count": existing.get("m1_item_count"),
            "item_count": existing.get("item_count"),
            "merge_operations": existing.get("merge_operations"),
            "merged_clusters": existing.get("merged_clusters"),
            "project_entity_count": existing.get("project_entity_count"),
            "validation_status": existing.get("validation_status"),
            "issue_count": existing.get("issue_count"),
            "llm_calls": 0,
            "elapsed_ms": 0,
        }

    # ---- 入口校验：M1 九字段（不合规直接拒，不进 M2）----------------
    m1_errors = _schema_errors(M1_ITEMS_SCHEMA, {"items": items})
    if m1_errors:
        archived = _preserve({"items": items}, "m1_input_rejected")
        logger.warning(
            "M1 输入 Schema 校验失败 doc=%s（%d 项），已留档 %s",
            source_document_id, len(m1_errors), archived,
        )
        raise M2Error(
            422,
            "m1_items schema validation failed（输入侧）",
            errors=m1_errors,
            archived=archived,
            source_document_id=source_document_id,
        )

    with _run_lock():
        started = time.monotonic()
        _ensure_data_dirs()
        client = None if no_llm else LLMClient(LLMConfig.from_env())
        # catalog 每次新建实例（同一个持久化文件）：
        # 既让 alias / entity_id 跨会议累积，又避开 sqlite3 连接跨线程复用的限制。
        catalog = ProjectCatalog(str(catalog_path or CATALOG_PATH))
        try:
            result = run_pipeline(
                {"items": items},
                source_mode=source_mode,
                client=client,
                catalog=catalog,
                normalized_text=None,
                workers=workers,
                generate_titles=not no_llm,
            )
        except LLMError as error:
            logger.exception("LLM 调用失败 doc=%s", source_document_id)
            raise M2Error(500, "模型调用失败：{}".format(error))
        except (ValueError, RuntimeError) as error:
            logger.exception("M2 流水线运行失败 doc=%s", source_document_id)
            raise M2Error(500, "M2 流水线失败：{}".format(error))
        finally:
            try:
                catalog.conn.close()
            except Exception:  # noqa: BLE001 - 关连接失败不影响主流程
                pass
        elapsed_ms = int((time.monotonic() - started) * 1000)

    payload = result.payload()

    # ---- 出口校验：M2 自身 Schema + 质量门 --------------------------
    m2_errors = _schema_errors(M2_OUTPUT_SCHEMA, payload)
    status = result.validation["status"]
    if (m2_errors or status == "ERROR") and not allow_invalid:
        archived = _preserve(
            {"payload": payload, "report": result.report(), "schema_errors": m2_errors},
            "m2_output_rejected",
        )
        logger.warning(
            "M2 输出未过质量门 doc=%s status=%s schema_errors=%d，不落业务表，已留档 %s",
            source_document_id, status, len(m2_errors), archived,
        )
        raise M2Error(
            422,
            "m2 质量门 {} / Schema 校验失败，拒绝入库（不静默修正）".format(status),
            validation_status=status,
            schema_errors=m2_errors,
            issue_counts=result.validation.get("issue_counts", {}),
            archived=archived,
            source_document_id=source_document_id,
        )

    # ---- 权威源落盘（红线：权威源是 M2 JSON，不是中台表）-------------
    # 用字符串拼接而不是 Path.with_suffix：doc id 形如 2026-04-07，
    # with_suffix(".json") 会把 ".m2" 当成后缀换掉，得到 2026-04-07.json。
    # 命名沿用 M2 自己 CLI 的约定：<doc>.m2.json / .m2.report.json / .m2.review.json。
    prefix = str(M2_OUT_DIR / (_safe_name(source_document_id) + ".m2"))
    m2_json_path = _write_json(Path(prefix + ".json"), payload)
    report_path = _write_json(Path(prefix + ".report.json"), result.report())
    review_path = None
    if result.review:
        review_path = _write_json(Path(prefix + ".review.json"), result.review)

    # ---- 落库 ------------------------------------------------------
    trace = _align_trace(payload["items"], payload["merge_trace"])
    llm_stats = result.stats.get("llm") or {}
    doc_row = {
        "source_document_id": source_document_id,
        "file_name": document.get("file_name") or source_document_id + ".items.json",
        "meeting_date": document.get("meeting_date")
        or meeting_date_from_doc_id(source_document_id),
        "source_mode": source_mode,
        "m1_item_count": len(items),
        "item_count": len(payload["items"]),
        "merge_operations": int(result.stats.get("merge_operations") or 0),
        "merged_clusters": int(result.stats.get("merged_clusters") or 0),
        "project_entity_count": len(payload["project_entities"]),
        "validation_status": status,
        "issue_count": len(result.validation.get("issues") or []),
        "input_fingerprint": fingerprint,
        "llm_calls": int(llm_stats.get("calls") or 0),
        "elapsed_ms": elapsed_ms,
        "report_json": json.dumps(result.report(), ensure_ascii=False),
    }
    upsert = repository.upsert_consolidation(
        doc_row=doc_row,
        items=payload["items"],
        trace=trace,
        project_entities=payload["project_entities"],
        issues=result.validation.get("issues") or [],
    )

    logger.info(
        "consolidate ok doc=%s mode=%s m1_items=%d m2_items=%d merged_clusters=%d "
        "status=%s llm_calls=%s elapsed=%dms",
        source_document_id, source_mode, len(items), len(payload["items"]),
        doc_row["merged_clusters"], status, doc_row["llm_calls"], elapsed_ms,
    )
    return {
        "status": "ok",
        "skipped": False,
        "source_document_id": source_document_id,
        "source_mode": source_mode,
        "m1_item_count": len(items),
        "item_count": len(payload["items"]),
        "merge_operations": doc_row["merge_operations"],
        "merged_clusters": doc_row["merged_clusters"],
        "project_entity_count": doc_row["project_entity_count"],
        "validation_status": status,
        "issue_counts": result.validation.get("issue_counts", {}),
        "schema_errors": m2_errors,
        "llm_calls": doc_row["llm_calls"],
        "elapsed_ms": elapsed_ms,
        "m2_json": m2_json_path,
        "report_json": report_path,
        "review_json": review_path,
        "upsert": upsert,
    }


# --------------------------------------------------------------------------
# 批量：增量 / 全量
# --------------------------------------------------------------------------


def _brief(row: dict) -> dict:
    """批量结果里每份文档只带短字段，避免响应体过大（顿悟输出节点也吃不下）。"""
    return {
        "source_document_id": row.get("source_document_id"),
        "status": row.get("status"),
        "m1_item_count": row.get("m1_item_count"),
        "item_count": row.get("item_count"),
        "merge_operations": row.get("merge_operations"),
        "validation_status": row.get("validation_status"),
        "elapsed_ms": row.get("elapsed_ms"),
    }


def _run_batch(
    repository: M2Repository,
    doc_ids: List[str],
    workers: int,
    force: bool,
    no_llm: bool,
    allow_invalid: bool,
    reason_by_doc: Optional[Dict[str, str]] = None,
    catalog_path: Optional[str] = None,
) -> dict:
    started = time.monotonic()
    processed: List[dict] = []
    skipped: List[dict] = []
    failures: List[dict] = []

    with _run_lock():
        for doc_id in doc_ids:
            try:
                row = consolidate_document(
                    repository,
                    doc_id,
                    workers=workers,
                    force=force,
                    no_llm=no_llm,
                    allow_invalid=allow_invalid,
                    catalog_path=catalog_path,
                )
            except M2Error as error:
                # 批量里单份失败不静默吞掉：记下来，最后整体以非 2xx 上抛，
                # 这样顿悟 HTTP 节点的 raise_for_status 会把节点标成 failed。
                failures.append(
                    {
                        "source_document_id": doc_id,
                        "status_code": error.status_code,
                        "detail": error.detail,
                        **{k: v for k, v in error.extra.items() if k != "archived"},
                        "archived": error.extra.get("archived"),
                    }
                )
                continue
            (skipped if row.get("skipped") else processed).append(_brief(row))

    totals = {
        "m1_item_count": sum(r.get("m1_item_count") or 0 for r in processed),
        "item_count": sum(r.get("item_count") or 0 for r in processed),
        "merge_operations": sum(r.get("merge_operations") or 0 for r in processed),
    }
    summary = {
        "processed": len(processed),
        "skipped": len(skipped),
        "failed": len(failures),
        "documents": processed,
        "skipped_documents": skipped,
        "totals": totals,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "pending_left": len(repository.list_pending()),
    }
    if reason_by_doc:
        summary["reasons"] = reason_by_doc

    if failures:
        codes = {f["status_code"] for f in failures}
        raise M2Error(
            422 if codes == {422} else 500,
            "批量归并有 {} 份失败（成功 {} 份、跳过 {} 份）".format(
                len(failures), len(processed), len(skipped)
            ),
            failures=failures,
            **summary,
        )
    summary["status"] = "ok"
    return summary


def consolidate_pending(
    repository: M2Repository,
    limit: Optional[int] = 1,
    workers: int = 1,
    no_llm: bool = False,
    allow_invalid: bool = False,
    catalog_path: Optional[str] = None,
) -> dict:
    """增量：只跑「从未归并」或「M1 输入已变更」的文档。

    顿悟工作流走这个端点——静态 URL、无 body、limit 走 query，
    正好绕开平台 HTTP 节点不插值 body/queryParams 的缺陷。
    """
    pending = repository.list_pending(limit=limit)
    if not pending:
        return {
            "status": "ok",
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "documents": [],
            "skipped_documents": [],
            "totals": {"m1_item_count": 0, "item_count": 0, "merge_operations": 0},
            "elapsed_ms": 0,
            "pending_left": 0,
        }
    return _run_batch(
        repository,
        [row["source_document_id"] for row in pending],
        workers=workers,
        force=True,  # 已按指纹筛过，这里必须真跑
        no_llm=no_llm,
        allow_invalid=allow_invalid,
        reason_by_doc={row["source_document_id"]: row["reason"] for row in pending},
        catalog_path=catalog_path,
    )


def consolidate_all(
    repository: M2Repository,
    workers: int = 1,
    force: bool = False,
    no_llm: bool = False,
    allow_invalid: bool = False,
    catalog_path: Optional[str] = None,
) -> dict:
    """全量：按 source_document_id 升序（= 会议时间序）逐份归并。

    顺序很重要：项目主表 catalog 跨文档累积 alias，先跑的会议会给后面的
    会议提供实体命中，换个顺序 entity_id 分配就变了。
    """
    documents = repository.list_documents()
    if not documents:
        raise M2Error(404, "ods_m1_source_documents 为空，没有可归并的 M1 输出")
    return _run_batch(
        repository,
        [row["source_document_id"] for row in documents],
        workers=workers,
        force=force,
        no_llm=no_llm,
        allow_invalid=allow_invalid,
        catalog_path=catalog_path,
    )
