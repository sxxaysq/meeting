# -*- coding: utf-8 -*-
"""M2 语义归并服务（端口 18093）。

职责（仿 m1_service / m4_bidding 的 HTTP 壳模式，不复制业务逻辑）：
- 从数据中台 `ods_m1_meeting_items` 拉取 M1 九字段输出作为输入（**只 SELECT**）；
- import 调用 `M2_SemanticConsolidator/src` 的 run_pipeline 做保守语义归并；
- 权威 JSON 落盘 `data/m2/<doc>.m2.json`，结构化结果单事务幂等写进 `ods_m2_*` 四表。

错误语义对齐 M1/M4 传统：
- 输入/输出 Schema 校验失败、质量门 ERROR → 422，原始 payload 留档，不静默修正；
- 文档不存在 → 404；来源模式非法/无条目 → 400；
- 已有归并任务在跑 → 409（项目主表 catalog 非并发安全，不排队）；
- LLM 或流水线异常 → 500，完整 traceback 入日志。

顿悟智体平台适配（平台 HTTP 节点缺陷未修，见 integration/dunwu_http_node_bug.md）：
- 缺陷 1/2：`bodyType` 被忽略、字符串 body 不插值 → 本服务的 POST 端点
  **body 可为空**，`/m2/run-pending` 靠静态 query `limit` 驱动，不需要 body；
- 缺陷 2 的另一半：平台把 body 发成 JSON 字符串字面量 → `_read_payload` 再解一层；
- 缺陷 3：占位符解析失败会原样发出 `{{...}}` → 含 `{{` 的取值一律忽略；
- headers 是平台唯一会插值的通道 → 额外支持 `X-Source-Document-Id` 指定单文档。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import runner
from repository import build_repository
from runner import M2Error

HERE = Path(__file__).resolve().parent
DEFAULT_DSN = "sqlite:///" + str(HERE / "data" / "m2_staging.db")
DEFAULT_WORKERS = int(os.getenv("M2_WORKERS", "4"))
DOC_ID_HEADER = "X-Source-Document-Id"

logger = logging.getLogger("m2_service")


# --------------------------------------------------------------------------
# 请求解析（含顿悟平台容错）
# --------------------------------------------------------------------------


async def _read_payload(request: Request) -> dict:
    """把请求体解析成 dict，容忍顿悟 HTTP 节点的两种畸形发送方式。

    - body 为空（平台 Body 类型选 none）→ 返回 {}；
    - body 是 JSON 字符串字面量（平台把字符串 body 交给 httpx 的 json=）→ 再解一层。
    """
    raw = await request.body()
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise M2Error(400, "请求体不是合法 JSON：{}".format(error))
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as error:
            raise M2Error(
                400, "请求体是 JSON 字符串字面量，但内层不是合法 JSON：{}".format(error)
            )
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise M2Error(400, "请求体期望是 JSON 对象，实际是 {}".format(type(payload).__name__))
    return payload


def _usable(value: Any) -> Optional[str]:
    """取值可用性：非空、是字符串、且不是平台未解析的 {{占位符}} 原文。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or "{{" in value:
        return None
    return value


def _doc_id(request: Request, payload: dict) -> Optional[str]:
    """body 优先，其次 header（顿悟唯一可插值通道）。"""
    return _usable(payload.get("source_document_id")) or _usable(
        request.headers.get(DOC_ID_HEADER)
    )


def _flag(payload: dict, key: str, query: Optional[bool] = None) -> bool:
    if query is not None:
        return bool(query)
    return bool(payload.get(key, False))


def _int(payload: dict, key: str, query: Optional[int], default: int) -> int:
    value = query if query is not None else payload.get(key, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        raise M2Error(400, "{} 必须是整数，收到 {!r}".format(key, value))


def _as_response(error: M2Error) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code, content={"detail": error.detail, **error.extra}
    )


# --------------------------------------------------------------------------
# 应用工厂
# --------------------------------------------------------------------------


def create_app(dsn: Optional[str] = None) -> FastAPI:
    dsn = dsn or os.getenv("M2_DSN", DEFAULT_DSN)
    repository = build_repository(dsn, str(HERE))
    # 启动即确保 ods_m2_* 四表四视图存在（DDL 全部幂等）；连不上库就在这里炸，
    # 不会带着病上线。
    repository.bootstrap_schema()

    app = FastAPI(title="M2 Semantic Consolidation Service", version="1.0.0")
    app.state.repository = repository
    app.state.dsn = dsn

    @app.exception_handler(M2Error)
    async def _handle_m2_error(request: Request, error: M2Error) -> JSONResponse:
        """统一把 M2Error 映射成对应状态码。

        必须用应用级处理器而不是各端点 try/except：_read_payload / _int 这些
        在端点体内、try 块外抛出的 M2Error，没人接就会变成 500。
        """
        return _as_response(error)

    @app.get("/healthz")
    def healthz() -> dict:
        """永不 500，也不走重查询（对齐 m1_staging / m4 的 healthz 语义）。"""
        return {
            "status": "ok",
            "dialect": repository.dialect,
            "dsn_scheme": dsn.split(":")[0],
            "catalog": str(runner.CATALOG_PATH),
            "m2_out_dir": str(runner.M2_OUT_DIR),
            "default_workers": DEFAULT_WORKERS,
            "llm": runner.llm_backend(),
        }

    @app.get("/m2/pending")
    def pending(limit: Optional[int] = None) -> dict:
        """待归并清单（只读）：从未归并、或 M1 输入指纹已变更的文档。"""
        documents = repository.list_documents()
        rows = repository.list_pending(limit=limit)
        return {
            "status": "ok",
            "m1_document_count": len(documents),
            "pending_count": len(rows),
            "pending": rows,
        }

    @app.get("/m2/stats")
    def stats(source_document_id: str) -> dict:
        return repository.stats(source_document_id)

    @app.post("/m2/consolidate")
    async def consolidate(
        request: Request,
        workers: Optional[int] = None,
        no_llm: Optional[bool] = None,
        force: Optional[bool] = None,
        allow_invalid: Optional[bool] = None,
    ) -> JSONResponse:
        """归并指定的一份会议文档。doc id 取 body，其次取 X-Source-Document-Id 头。"""
        payload = await _read_payload(request)
        doc_id = _doc_id(request, payload)
        if not doc_id:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "缺少 source_document_id：请放在 JSON body 里，"
                    "或放在 {} 请求头里（顿悟工作流走后者）".format(DOC_ID_HEADER)
                },
            )
        result = await run_in_threadpool(
            runner.consolidate_document,
            repository,
            doc_id,
            _int(payload, "workers", workers, DEFAULT_WORKERS),
            _flag(payload, "force", force),
            _flag(payload, "no_llm", no_llm),
            _flag(payload, "allow_invalid", allow_invalid),
        )
        return JSONResponse(status_code=200, content=result)

    @app.post("/m2/run-pending")
    async def run_pending(
        request: Request,
        limit: Optional[int] = None,
        workers: Optional[int] = None,
        no_llm: Optional[bool] = None,
        allow_invalid: Optional[bool] = None,
    ) -> JSONResponse:
        """增量归并：只跑「从未归并」或「M1 输入已变更」的文档。

        顿悟工作流走这个端点——静态 URL + 静态 query `limit`，body 可以完全不填，
        正好绕开平台 HTTP 节点不插值 body / queryParams 的缺陷。
        """
        payload = await _read_payload(request)
        result = await run_in_threadpool(
            runner.consolidate_pending,
            repository,
            _int(payload, "limit", limit, 1),
            _int(payload, "workers", workers, DEFAULT_WORKERS),
            _flag(payload, "no_llm", no_llm),
            _flag(payload, "allow_invalid", allow_invalid),
        )
        return JSONResponse(status_code=200, content=result)

    @app.post("/m2/consolidate-all")
    async def consolidate_all(
        request: Request,
        workers: Optional[int] = None,
        no_llm: Optional[bool] = None,
        force: Optional[bool] = None,
        allow_invalid: Optional[bool] = None,
    ) -> JSONResponse:
        """全量归并（初始化与幂等回归用）。按会议时间序逐份跑，耗时随文档数线性增长。"""
        payload = await _read_payload(request)
        result = await run_in_threadpool(
            runner.consolidate_all,
            repository,
            _int(payload, "workers", workers, DEFAULT_WORKERS),
            _flag(payload, "force", force),
            _flag(payload, "no_llm", no_llm),
            _flag(payload, "allow_invalid", allow_invalid),
        )
        return JSONResponse(status_code=200, content=result)

    return app


app = create_app()


def main() -> int:
    import uvicorn

    uvicorn.run(
        "service:app",
        host=os.getenv("M2_SERVICE_HOST", "0.0.0.0"),
        port=int(os.getenv("M2_SERVICE_PORT", "18093")),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except M2Error as error:
        print("M2 归并失败（不静默修正）：{}".format(error.detail), file=sys.stderr)
        raise SystemExit(2)
