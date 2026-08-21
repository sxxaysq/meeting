# -*- coding: utf-8 -*-
"""§5.4 验收用例：cron 兜底扫描的增量语义。"""

import json
import sys
from pathlib import Path

from conftest import STAGING_DIR, make_item

sys.path.insert(0, str(STAGING_DIR))

from cron_scan import scan_once  # noqa: E402
from repository import build_repository  # noqa: E402


def _write(path: Path, items) -> None:
    path.write_text(
        json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8"
    )


def test_cron_scan_incremental(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write(out_dir / "2026-04-07.items.json", [make_item()])
    _write(out_dir / "2026-04-13.items.json", [make_item(), make_item(title="B", content="BB")])
    (out_dir / "2026-04-07.report.json").write_text("{}", encoding="utf-8")  # 非目标后缀应被忽略

    dsn = "sqlite:///" + str(tmp_path / "scan.db")
    repository = build_repository(dsn, str(tmp_path))
    state_path = tmp_path / "state.json"

    # 第一轮：全部入库
    round1 = scan_once(out_dir, repository, state_path)
    assert len(round1["ingested"]) == 2
    assert round1["failed"] == []
    assert repository.stats("2026-04-07")["item_count"] == 1
    assert repository.stats("2026-04-13")["item_count"] == 2

    # 第二轮：内容未变 → 全部跳过，行数不变
    round2 = scan_once(out_dir, repository, state_path)
    assert round2["ingested"] == []
    assert len(round2["skipped"]) == 2
    assert repository.stats("2026-04-07")["item_count"] == 1

    # 第三轮：文件内容变化 → 重新 ingest（幂等 upsert，不产生重复行）
    _write(
        out_dir / "2026-04-13.items.json",
        [make_item(), make_item(title="B", content="BB"), make_item(title="C", content="CC")],
    )
    round3 = scan_once(out_dir, repository, state_path)
    assert round3["ingested"] == [str(out_dir / "2026-04-13.items.json")]
    assert repository.stats("2026-04-13")["item_count"] == 3
    repository.close()


def test_cron_scan_bad_file_isolated(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write(out_dir / "good.items.json", [make_item()])
    (out_dir / "bad.items.json").write_text("{\"items\": \"not-a-list\"}", encoding="utf-8")

    dsn = "sqlite:///" + str(tmp_path / "scan.db")
    repository = build_repository(dsn, str(tmp_path))
    state_path = tmp_path / "state.json"
    result = scan_once(out_dir, repository, state_path)
    assert repository.stats("good")["item_count"] == 1, "坏文件不应阻塞好文件"
    assert len(result["failed"]) == 1
    repository.close()
