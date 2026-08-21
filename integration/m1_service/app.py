# -*- coding: utf-8 -*-
"""Track B：M1 执行服务（端口 8091）。

用 FastAPI 包装 M1_Extraction/src 的既有流水线（import 调用，不复制逻辑），
供顿悟工作流的 HTTP 节点调用，保留 PyMuPDF 真实页码路径（Track A 页码恒 null 的补集）。

错误语义对齐流水线传统：
- Schema 校验失败 → 422，原始输出留档日志，不静默修正、不吞异常；
- 进程级异常 → 500，完整 traceback 入日志。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

HERE = Path(__file__).resolve().parent
M1_ROOT = HERE.parents[1] / "meeting-m2-work" / "M1_Extraction"
sys.path.insert(0, str(M1_ROOT / "src"))

from llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402
from pipeline import run_pipeline  # noqa: E402

REJECT_DIR = HERE / "data" / "rejected"

logging.basicConfig(
    level=os.getenv("M1_SERVICE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("m1_service")

app = FastAPI(title="M1 Extraction Service", version="1.0.0")


def _preserve(payload, label: str) -> str:
    import time

    REJECT_DIR.mkdir(parents=True, exist_ok=True)
    path = REJECT_DIR / "{}_{}.json".format(label, int(time.time() * 1000))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


@app.get("/healthz")
def healthz() -> dict:
    try:
        config = LLMConfig.from_env()
        backend = {"base_url": config.base_url, "model": config.model, "transport": config.transport}
    except Exception as error:  # noqa: BLE001 - healthz 永不 500
        backend = {"error": str(error)}
    return {"status": "ok", "m1_root": str(M1_ROOT), "llm": backend}


@app.post("/m1/extract")
def extract(
    file: UploadFile = File(...),
    mode: str = Form("block"),
    max_block_chars: int = Form(1200),
    workers: int = Form(1),
) -> JSONResponse:
    """multipart 上传一份会议文件，返回 {"items": [...]}（九字段，坐标由代码算出）。"""
    if mode not in ("block", "generic"):
        return JSONResponse(status_code=400, content={"detail": "mode 仅支持 block|generic"})

    suffix = Path(file.filename or "upload.bin").suffix or ".bin"
    tmp_dir = tempfile.mkdtemp(prefix="m1_service_")
    tmp_path = Path(tmp_dir) / ("input" + suffix)
    try:
        with tmp_path.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)

        config = LLMConfig.from_env()
        logger.info(
            "extract start file=%s mode=%s model=%s base_url=%s",
            file.filename, mode, config.model, config.base_url,
        )
        client = LLMClient(config)
        result = run_pipeline(
            str(tmp_path),
            client=client,
            max_block_chars=max_block_chars,
            workers=workers,
            mode=mode,
        )

        if result.schema_errors:
            archived = _preserve(result.items_payload(), "schema_rejected")
            logger.warning(
                "Schema 校验失败，返回 422（诊断已留档 %s）：%s",
                archived,
                "; ".join(result.schema_errors[:20]),
            )
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "m1 schema validation failed",
                    "errors": result.schema_errors[:20],
                    "archived": archived,
                },
            )

        payload = result.items_payload()
        report = result.report()
        logger.info(
            "extract ok file=%s items=%d exact_match=%.1f%%",
            file.filename,
            report["item_count"],
            report["exact_match_rate"] * 100,
        )
        return JSONResponse(
            status_code=200,
            content={
                "items": payload["items"],
                "mode": mode,
                "source_document_id": "doc:" + (file.filename or "unknown"),
            },
        )
    except LLMError as error:
        # 模型侧失败：不吞，500 上抛，日志保留完整信息
        logger.exception("LLM 调用失败")
        return JSONResponse(status_code=500, content={"detail": "模型调用失败：{}".format(error)})
    except RuntimeError as error:
        logger.exception("流水线运行失败")
        return JSONResponse(status_code=500, content={"detail": str(error)})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.getenv("M1_SERVICE_HOST", "0.0.0.0"),
        port=int(os.getenv("M1_SERVICE_PORT", "8091")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
