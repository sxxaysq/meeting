# -*- coding: utf-8 -*-
"""M4 bidding 分离服务（端口 8092）。

职责（仿 m1_service HTTP 壳模式，不复制业务逻辑）：
- 接收 m1 九字段 items（先走 m1_items.schema.json 严格校验，失败 422 留档）；
- LLM 招投标分类（classifier.py），招投标条目单事务幂等 upsert 进 m4 表；
- 返回 bidding / filtered 两路结果：filtered 替代 m1 原始输出进入 m1 原有下游，
  bidding 后续单独进 m3/m6。

错误语义对齐 M1 传统：
- Schema 校验失败 → 422，原始 payload 留档，不静默修正；
- LLM 分类失败/输出非法 → 500，完整 traceback 入日志。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from classifier import ClassificationError, classify_items, cross_check, split_items  # noqa: E402
from repository import (  # noqa: E402
    build_repository,
    date_from_doc_id,
    doc_id_from_path,
    meeting_date_from_doc_id,
)

M1_ROOT = HERE.parents[1] / "M1_Extraction"
sys.path.insert(0, str(M1_ROOT / "src"))

from llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402

SCHEMA_PATH = M1_ROOT / "schemas" / "m1_items.schema.json"
DEFAULT_DSN = "sqlite:///" + str(HERE / "data" / "m4_bidding.db")
REJECT_DIR = HERE / "data" / "rejected"

logging.basicConfig(
    level=os.getenv("M4_SERVICE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("m4_service")


class SplitRequest(BaseModel):
    """传输信封。业务严格性由 m1_items jsonschema 负责（同 m1_staging 做法）。"""

    source_document_id: Optional[str] = None
    file_name: Optional[str] = None
    meeting_date: Optional[str] = None
    mode: Optional[str] = "generic"
    chunk_size: int = 80
    items: List[Any]


class SplitFileRequest(BaseModel):
    path: str
    source_document_id: Optional[str] = None
    meeting_date: Optional[str] = None
    mode: Optional[str] = "generic"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _preserve(payload, label: str) -> str:
    REJECT_DIR.mkdir(parents=True, exist_ok=True)
    path = REJECT_DIR / "{}_{}.json".format(label, int(time.time() * 1000))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def create_app(dsn: Optional[str] = None) -> FastAPI:
    dsn = dsn or os.getenv("M4_BIDDING_DSN", DEFAULT_DSN)
    repository = build_repository(dsn, str(HERE))
    import jsonschema

    validator = jsonschema.Draft7Validator(_load_schema())

    app = FastAPI(title="M4 Bidding Split Service", version="1.0.0")
    app.state.repository = repository

    @app.get("/healthz")
    def healthz() -> dict:
        try:
            config = LLMConfig.from_env()
            backend = {"base_url": config.base_url, "model": config.model, "transport": config.transport}
        except Exception as error:  # noqa: BLE001 - healthz 永不 500
            backend = {"error": str(error)}
        return {"status": "ok", "dialect": repository.dialect, "llm": backend}

    def _do_split(source_document_id: str, file_name: str, meeting_date: Optional[str],
                  mode: str, chunk_size: int, items: List[Any]) -> JSONResponse:
        # doc id 先向 M1 已登记的口径归一：否则 origin_item_id 用错的 doc_id 派生，
        # 血缘 JOIN 不回 ods_m1_meeting_items（实测踩过 8.24 那份）。
        requested_id = source_document_id
        source_document_id = repository.resolve_source_document_id(source_document_id)
        if source_document_id != requested_id:
            logger.info(
                "doc id 归一：%r -> %r（对齐 ods_m1_source_documents）",
                requested_id, source_document_id,
            )
        payload = {"items": items}
        errors = sorted(validator.iter_errors(payload), key=lambda e: [str(p) for p in e.path])
        if errors:
            messages = [
                "{}: {}".format("/".join(str(p) for p in e.path) or "$", e.message)
                for e in errors[:20]
            ]
            archived = _preserve(payload, "m4_schema_rejected")
            logger.warning(
                "m1 Schema 校验失败，拒绝处理（%d 项问题）：已留档 %s", len(errors), archived
            )
            return JSONResponse(
                status_code=422,
                content={"detail": "m1_items schema validation failed", "errors": messages,
                         "archived": archived},
            )
        client = LLMClient(LLMConfig.from_env())
        classified = classify_items(items, client, chunk_size=chunk_size)
        annotations = classified["annotations"]
        split = split_items(items, annotations)
        disagreements = cross_check(items, annotations)
        upsert = repository.upsert_bidding(
            source_document_id=source_document_id,
            file_name=file_name,
            # 先严格 YYYY-MM-DD，再退化到从文件名形态（2026.8.24…）里确定性取日期；
            # 两者都提不出来才置 NULL，不猜。
            meeting_date=meeting_date
            or meeting_date_from_doc_id(source_document_id)
            or date_from_doc_id(file_name)
            or date_from_doc_id(source_document_id),
            mode=mode or "generic",
            items=items,
            annotations={idx: annotations[idx] for idx in classified["bidding_indices"]},
        )
        logger.info(
            "split ok doc=%s items=%d bidding=%d filtered=%d",
            source_document_id, len(items), len(split["bidding"]), len(split["filtered"]),
        )
        return JSONResponse(
            status_code=200,
            content={
                "source_document_id": source_document_id,
                "item_count": len(items),
                "bidding_count": len(split["bidding"]),
                "filtered_count": len(split["filtered"]),
                "upsert": upsert,
                "disagreements": disagreements,
                "bidding": split["bidding"],
                "filtered": split["filtered"],
                "llm_stats": classified["llm_stats"],
            },
        )

    @app.post("/m4/split")
    def split(request: SplitRequest) -> JSONResponse:
        source_document_id = request.source_document_id or "doc:" + (request.file_name or "unknown")
        return _do_split(
            source_document_id=source_document_id,
            file_name=request.file_name or source_document_id + ".items.json",
            meeting_date=request.meeting_date,
            mode=request.mode or "generic",
            chunk_size=request.chunk_size,
            items=request.items,
        )

    @app.post("/m4/split-file")
    def split_file(request: SplitFileRequest) -> JSONResponse:
        path = Path(request.path)
        if not path.exists() or not path.is_file():
            return JSONResponse(status_code=400, content={"detail": "文件不存在：{}".format(path)})
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            return JSONResponse(status_code=400, content={"detail": "不是合法 JSON：{}".format(error)})
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return JSONResponse(
                status_code=400, content={"detail": "期望形如 {\"items\": [...]} 的 m1 输出文件"}
            )
        source_document_id = request.source_document_id or doc_id_from_path(path)
        return _do_split(
            source_document_id=source_document_id,
            file_name=path.name,
            meeting_date=request.meeting_date,
            mode=request.mode or "generic",
            chunk_size=80,
            items=payload["items"],
        )

    @app.get("/m4/stats")
    def stats(source_document_id: str) -> dict:
        # 查询侧也归一：传 2026-08-24 与传 M1 登记的文件名形态应得到同一份结果。
        resolved = repository.resolve_source_document_id(source_document_id)
        result = repository.stats(resolved)
        if resolved != source_document_id:
            # 如实告知调用方真正命中的是哪个 id，不静默改写
            result["requested_source_document_id"] = source_document_id
        return result

    return app


app = create_app()


def main() -> int:
    import uvicorn

    uvicorn.run(
        "service:app",
        host=os.getenv("M4_SERVICE_HOST", "0.0.0.0"),
        port=int(os.getenv("M4_SERVICE_PORT", "8092")),
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ClassificationError as error:
        print("分类失败（不静默修正）：{}".format(error), file=sys.stderr)
        raise SystemExit(2)
    except LLMError as error:
        print("LLM 调用失败：{}".format(error), file=sys.stderr)
        raise SystemExit(3)
