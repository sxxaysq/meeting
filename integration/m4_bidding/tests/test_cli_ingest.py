# -*- coding: utf-8 -*-
"""cli.py --ingest-url 可选直推逻辑测试（monkeypatch HTTP，无网络）。"""

import sys
from pathlib import Path

# 全量跑时 service/classifier 会把 M1 src 插到 sys.path 最前，
# 必须把 m4 目录重新插到最前，确保 import cli 解析到本组件的 cli.py。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import FakeLLMClient, make_bidding_item, make_item

import cli


def test_process_items_without_ingest_url(tmp_path, sqlite_repo, monkeypatch):
    """缺省不传 --ingest-url：不触发任何 HTTP 调用，报告无 ingest 字段。"""
    called = []
    monkeypatch.setattr(cli, "_post_ingest", lambda url, payload: called.append(url))
    items = [make_bidding_item(), make_item()]
    report = cli.process_items(
        items=items,
        client=FakeLLMClient({0: (True, "TENDER", "编制招标文件")}),
        repository=sqlite_repo,
        source_document_id="2026-04-07",
        file_name="2026-04-07.items.json",
        meeting_date="2026-04-07",
        mode="generic",
        out_dirs={"bidding": tmp_path, "filtered": tmp_path, "reports": tmp_path},
        chunk_size=80,
    )
    assert called == []
    assert "ingest" not in report
    assert report["bidding_count"] == 1


def test_process_items_with_ingest_url(tmp_path, sqlite_repo, monkeypatch):
    """传 --ingest-url：filtered 九字段 payload 被直推，结果进报告。"""
    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return {"status": "ok", "item_count": len(payload["items"])}

    monkeypatch.setattr(cli, "_post_ingest", fake_post)
    items = [make_bidding_item(), make_item()]
    report = cli.process_items(
        items=items,
        client=FakeLLMClient({0: (True, "TENDER", "编制招标文件")}),
        repository=sqlite_repo,
        source_document_id="2026-04-07",
        file_name="2026-04-07.items.json",
        meeting_date="2026-04-07",
        mode="generic",
        out_dirs={"bidding": tmp_path, "filtered": tmp_path, "reports": tmp_path},
        chunk_size=80,
        ingest_url="http://127.0.0.1:8090/m1/ingest",
    )
    assert captured["url"] == "http://127.0.0.1:8090/m1/ingest"
    payload = captured["payload"]
    assert payload["source_document_id"] == "2026-04-07"
    # 只推 filtered（剔除招投标后），不含招投标条目
    assert len(payload["items"]) == 1
    assert payload["items"][0]["title"] == make_item()["title"]
    assert report["ingest"] == {"status": "ok", "item_count": 1}
