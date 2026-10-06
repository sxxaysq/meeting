# -*- coding: utf-8 -*-
"""out 目录兜底扫描：把 CLI 批次产出的 *.items.json / *.m1.json 也送进 staging。

用法::

    python integration/m1_staging/cron_scan.py --dir M1_Extraction/out_v14/generic
    # 可选：--state 自定义状态文件；--once 单次执行（默认），交给 crontab 周期调用

增量语义：状态文件记录每个文件的内容 sha1；内容未变的文件跳过，
内容变化则重新 ingest（upsert 幂等，行数不会重复）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from repository import build_repository, doc_id_from_path  # noqa: E402

DEFAULT_STATE = HERE / "data" / "scan_state.json"
DEFAULT_DSN = "sqlite:///" + str(HERE / "data" / "m1_staging.db")
PATTERNS = ("*.items.json", "*.m1.json")


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def scan_once(
    out_dir: Path,
    repository,
    state_path: Path,
    mode: str = "generic",
    meeting_date: Optional[str] = None,
) -> dict:
    """扫描一轮。返回 {ingested, skipped, failed} 统计。"""
    state = load_state(state_path)
    files = sorted(
        {path for pattern in PATTERNS for path in out_dir.glob(pattern) if path.is_file()}
    )
    ingested, skipped, failed = [], [], []
    for path in files:
        content = path.read_bytes()
        digest = hashlib.sha1(content).hexdigest()
        key = str(path)
        if state.get(key, {}).get("sha1") == digest:
            skipped.append(key)
            continue
        try:
            payload = json.loads(content.decode("utf-8"))
            items = payload["items"] if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise ValueError("不是 {\"items\": [...]} 形态")
            # 稳定身份用文件名（去后缀）：与 /m1/ingest-file 默认规则一致
            repository.upsert_batch(
                source_document_id=doc_id_from_path(path),
                file_name=path.name,
                meeting_date=meeting_date,
                mode=mode,
                items=items,
            )
        except Exception as error:  # noqa: BLE001 - 单文件失败不阻塞其余文件
            failed.append({"path": key, "error": str(error)})
            continue
        state[key] = {"sha1": digest, "item_count": len(items)}
        ingested.append(key)
    save_state(state_path, state)
    return {"ingested": ingested, "skipped": skipped, "failed": failed}


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 out 目录兜底扫描入库")
    parser.add_argument("--dir", required=True, help="扫描目录（如 M1_Extraction/out_v14/generic）")
    parser.add_argument("--dsn", default=None, help="缺省读环境变量 M1_STAGING_DSN")
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--mode", default="generic")
    parser.add_argument("--meeting-date", default=None)
    args = parser.parse_args()

    import os

    dsn = args.dsn or os.getenv("M1_STAGING_DSN", DEFAULT_DSN)
    repository = build_repository(dsn, str(HERE))
    result = scan_once(
        Path(args.dir),
        repository,
        Path(args.state),
        mode=args.mode,
        meeting_date=args.meeting_date,
    )
    print(
        "[cron_scan] ingested={} skipped={} failed={}".format(
            len(result["ingested"]), len(result["skipped"]), len(result["failed"])
        )
    )
    for item in result["failed"]:
        print("  失败：{} -> {}".format(item["path"], item["error"]), file=sys.stderr)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
