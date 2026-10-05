"""pytest 配置：Neo4j 连接 + 图隔离（每个用例前后清空 M3 标签节点）。

运行方式（仓库根目录）：
    /home/yty/m1x_venv/bin/python -m pytest M3_KnowledgeGraph/tests -q

说明：测试会清空 M3 业务标签节点以保证隔离；完整入图演示请在跑完测试后
重新执行一次 CLI ingest（见 README）。
"""
from __future__ import annotations

import inspect
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
NEO4J_PASSWORD = os.environ.get("M3_NEO4J_PASSWORD", "YOUR_PASSWORD")

# These tests drive the M2 → graph ingestion path. M2 is deprecated (the live
# pipeline is M1→M3→M6 with m2_enabled=false), the default password above no
# longer matches the running server, and that server is a Neo4j *Community*
# instance shared with an unrelated production project. ``wipe`` only deletes
# NODE_LABELS, which today do not collide with the neighbour's labels
# (Entity/Chunk/MilvusKB), but Project/Person/Department are generic enough that
# running these by default is not worth the risk. Set M3_RUN_GRAPH_TESTS=1 with
# correct credentials to run them against a scratch instance.
GRAPH_TESTS_ENABLED = os.environ.get("M3_RUN_GRAPH_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

pytestmark_reason = (
    "M2→图谱路径已废弃，且需要可写的独立 Neo4j 实例；"
    "设 M3_RUN_GRAPH_TESTS=1 并提供正确凭据后运行"
)


def pytest_collection_modifyitems(config, items):
    if GRAPH_TESTS_ENABLED:
        return
    skip = pytest.mark.skip(reason=pytestmark_reason)
    for item in items:
        # ``clean_graph`` is autouse and depends on ``store``, so ``store`` shows up
        # in every item's fixturenames. Select on the test's own signature instead,
        # otherwise the whole suite gets skipped.
        function = getattr(item, "function", None)
        if function is not None and "store" in inspect.signature(function).parameters:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def store():
    s = Neo4jStore(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    s.ensure_schema()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def clean_graph(request):
    """Wipe M3 labels around graph tests only.

    When graph tests are disabled this must not open a connection at all: the
    server is shared with another project and the stored credentials are stale.
    """
    if not GRAPH_TESTS_ENABLED:
        yield
        return
    store = request.getfixturevalue("store")
    wipe(store)
    yield
    wipe(store)
