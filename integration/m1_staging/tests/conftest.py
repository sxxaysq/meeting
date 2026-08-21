# -*- coding: utf-8 -*-
"""m1_staging 测试公共夹具。"""

import copy
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
STAGING_DIR = HERE.parent
sys.path.insert(0, str(STAGING_DIR))


def make_item(**overrides):
    """一个完全合法的九字段 item（坐标形态与真实 M1 输出一致）。"""
    item = {
        "department": "智能矿山事业部",
        "work_section": "经营工作",
        "delivery_group": None,
        "project": "智能综合管控平台",
        "item_type": "PROJECT_TASK",
        "assignee": ["张三", "李四"],
        "title": "完成管控平台数据接入",
        "content": "本周完成智能综合管控平台的数据接入与联调",
        "evidence": {
            "text": "完成智能综合管控平台的数据接入与联调",
            "page_start": 1,
            "page_end": 1,
            "start_char": 120,
            "end_char": 138,
            "exact_match": True,
        },
    }
    item.update(overrides)
    return item


def make_body(source_document_id="doc:test-001", items=None, **envelope):
    body = {
        "source_document_id": source_document_id,
        "file_name": "test-meeting.pdf",
        "meeting_date": "2026-04-07",
        "mode": "block",
        "items": items if items is not None else [make_item()],
    }
    body.update(envelope)
    return body


@pytest.fixture()
def client(tmp_path):
    """每个用例一个干净的 SQLite 库。"""
    from fastapi.testclient import TestClient

    from service import create_app

    dsn = "sqlite:///" + str(tmp_path / "staging.db")
    app = create_app(dsn=dsn)
    with TestClient(app) as test_client:
        yield test_client
    app.state.repository.close()


VALID_ITEM = make_item()
VALID_BODY = make_body()
