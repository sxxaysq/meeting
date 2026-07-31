"""M2 输入读取与原子输出。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


M1_REQUIRED_FIELDS = {
    "meeting_title",
    "segment_id",
    "speaker_id",
    "timestamp",
    "raw_text",
    "clean_text",
    "quality_flags",
    "uncertainties",
}


def load_m1_records(path: Path | str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("M1 输入必须是 JSON 数组")
    seen: set[str] = set()
    for index, record in enumerate(data):
        if not isinstance(record, dict):
            raise ValueError(f"M1 输入第 {index} 项不是对象")
        missing = sorted(M1_REQUIRED_FIELDS - record.keys())
        if missing:
            raise ValueError(f"M1 输入第 {index} 项缺少字段：{', '.join(missing)}")
        segment_id = record["segment_id"]
        if not isinstance(segment_id, str) or not segment_id:
            raise ValueError(f"M1 输入第 {index} 项 segment_id 无效")
        if segment_id in seen:
            raise ValueError(f"M1 输入存在重复 segment_id：{segment_id}")
        seen.add(segment_id)
        if not isinstance(record["clean_text"], str) or not record["clean_text"].strip():
            raise ValueError(f"M1 输入 {segment_id} 的 clean_text 为空")
    return data


def atomic_write_json(data: Any, path: Path | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
