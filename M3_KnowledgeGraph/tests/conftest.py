"""pytest 配置：Neo4j 连接 + 图隔离（每个用例前后清空 M3 标签节点）。

运行方式（仓库根目录）：
    /home/yty/m1x_venv/bin/python -m pytest M3_KnowledgeGraph/tests -q

说明：测试会清空 M3 业务标签节点以保证隔离；完整入图演示请在跑完测试后
重新执行一次 CLI ingest（见 README）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 保证以命名空间包方式导入 M3_KnowledgeGraph（仓库根目录入 sys.path）
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from M3_KnowledgeGraph.src.neo4j_store import Neo4jStore  # noqa: E402
from M3_KnowledgeGraph.tests.helpers import wipe  # noqa: E402

NEO4J_URI = os.environ.get("M3_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("M3_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("M3_NEO4J_PASSWORD", "m3graph2026")


@pytest.fixture(scope="session")
def store():
    s = Neo4jStore(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    s.ensure_schema()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def clean_graph(store):
    wipe(store)
    yield
    wipe(store)
