# -*- coding: utf-8 -*-
"""M1 staging-ingest 服务（端口 8090）。

职责：
- 对 M1 九字段 items 做严格 jsonschema 校验（复用 M1_Extraction/schemas/m1_items.schema.json，
  additionalProperties:false 生效），失败 422 不入库、不静默修正，原始 payload 保留到日志；
- 单事务 upsert 进 staging 库（MySQL 正式形态 / SQLite 自测形态，见 repository.py）；
- 重复 ingest 零重复行（稳定 item_id = "item:m1:" + sha1(source_document_id#idx)）。

红线提醒：本表是 ODS 贴源资产，权威源仍是 M2 JSON；中台/顿悟数据不得写回流水线。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import jsonschema

from repository import (
    StagingRepository,
    build_repository,
    derive_item_id,
    doc_id_from_path,
    meeting_date_from_doc_id,
)

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = (
    HERE.parents[1] / "meeting-m2-work" / "M1_Extraction" / "schemas" / "m1_items.schema.json"
)
DEFAULT_DSN = "sqlite:///" + str(HERE / "data" / "m1_staging.db")
REJECT_DIR = HERE / "data" / "rejected"

logging.basicConfig(
    level=os.getenv("M1_STAGING_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("m1_staging")


class IngestRequest(BaseModel):
    """传输信封。业务严格性由 jsonschema（九字段、additionalProperties:false）负责，
    因此 items 在这里只做结构承载，不做 pydantic 深校验，避免两套校验语义漂移。"""

    source_document_id: str
    file_name: str
    meeting_date: Optional[str] = None
    mode: Optional[str] = "generic"
    items: List[Any]


class IngestFileRequest(BaseModel):
    path: str
    source_document_id: Optional[str] = None
    file_name: Optional[str] = None
    meeting_date: Optional[str] = None
    mode: Optional[str] = "generic"


def load_items_schema() -> dict:
    if not SCHEMA_PATH.exists():
        raise RuntimeError("找不到 M1 Schema 文件：{}".format(SCHEMA_PATH))
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _preserve_rejected(payload: dict, reason: str) -> str:
    """422 的原始 payload 落盘留档，供人工排查（不进库）。"""
    import time

    REJECT_DIR.mkdir(parents=True, exist_ok=True)
    name = "rejected_{}_{}.json".format(int(time.time() * 1000), abs(hash(reason)) % 100000)
    path = REJECT_DIR / name
    path.write_text(
        json.dumps({"reason": reason, "payload": payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(path)


def create_app(dsn: Optional[str] = None) -> FastAPI:
    dsn = dsn or os.getenv("M1_STAGING_DSN", DEFAULT_DSN)
    repository: StagingRepository = build_repository(dsn, str(HERE))
    items_schema = load_items_schema()
    validator = jsonschema.Draft7Validator(items_schema)

    app = FastAPI(title="M1 Staging Ingest", version="1.0.0")
    app.state.repository = repository

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "dialect": repository.dialect, "dsn_scheme": dsn.split(":")[0]}

    @app.post("/m1/ingest")
    def ingest(request: IngestRequest) -> dict:
        payload = {"items": request.items}
        errors = sorted(validator.iter_errors(payload), key=lambda e: [str(p) for p in e.path])
        if errors:
            messages = [
                "{}: {}".format("/".join(str(p) for p in error.path) or "$", error.message)
                for error in errors[:20]
            ]
            archived = _preserve_rejected(request.model_dump(), "; ".join(messages))
            logger.warning(
                "Schema 校验失败，拒绝入库（%d 项问题）：%s；原始 payload 已留档 %s",
                len(errors),
                messages,
                archived,
            )
            return JSONResponse(
                status_code=422,
                content={"detail": "m1_items schema validation failed", "errors": messages},
            )
        result = repository.upsert_batch(
            source_document_id=request.source_document_id,
            file_name=request.file_name,
            # 未显式传会议日期且 doc id 形如 YYYY-MM-DD 时确定性兜底（本语料文件名约定）
            meeting_date=request.meeting_date
            or meeting_date_from_doc_id(request.source_document_id),
            mode=request.mode or "generic",
            items=request.items,
        )
        logger.info(
            "ingest ok source_document_id=%s items=%d", request.source_document_id, result["item_count"]
        )
        return {
            "status": "ok",
            "source_document_id": request.source_document_id,
            "item_count": result["item_count"],
            "item_ids": [
                derive_item_id(request.source_document_id, idx)
                for idx in range(len(request.items))
            ],
        }

    @app.post("/m1/ingest-file")
    def ingest_file(request: IngestFileRequest) -> dict:
        path = Path(request.path)
        if not path.exists() or not path.is_file():
            return JSONResponse(status_code=400, content={"detail": "文件不存在：{}".format(path)})
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            return JSONResponse(
                status_code=400, content={"detail": "不是合法 JSON：{}".format(error)}
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return JSONResponse(
                status_code=400, content={"detail": "期望形如 {\"items\": [...]} 的 M1 输出文件"}
            )
        # 未显式指定时用文件名（去 .items.json/.m1.json 后缀）做稳定身份：同一文件重扫必然幂等
        source_document_id = request.source_document_id or doc_id_from_path(path)
        file_name = request.file_name or path.name
        inner = IngestRequest(
            source_document_id=source_document_id,
            file_name=file_name,
            meeting_date=request.meeting_date,
            mode=request.mode,
            items=payload["items"],
        )
        return ingest(inner)

    @app.get("/m1/stats")
    def stats(source_document_id: str) -> dict:
        return repository.stats(source_document_id)

    return app


app = create_app()


def main() -> int:
    import uvicorn

    uvicorn.run(
        "service:app",
        host=os.getenv("M1_STAGING_HOST", "0.0.0.0"),
        port=int(os.getenv("M1_STAGING_PORT", "8090")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
