# -*- coding: utf-8 -*-
"""LLM 客户端：解析路径 + 与 M1 副本不漂移。"""

from pathlib import Path

import llm_client
from llm_client import LLMConfig, extract_json

M1_CLIENT = (
    Path(__file__).resolve().parents[2] / "M1_Extraction" / "src" / "llm_client.py"
)


def test_extract_json_tolerates_fence_and_think_block():
    assert extract_json('```json\n{"decision": "MERGE"}\n```') == {"decision": "MERGE"}
    assert extract_json('<think>推理</think>{"decision": "KEEP_SEPARATE"}') == {
        "decision": "KEEP_SEPARATE"
    }
    assert extract_json("前言 {\"a\": 1} 后记") == {"a": 1}
    assert extract_json("完全不是 JSON") is None
    assert extract_json("[1, 2]") is None  # 顶层必须是对象


def test_enable_thinking_env_semantics(monkeypatch):
    """空字符串 = 根本不发这个参数，给不认识它的服务端用。"""
    monkeypatch.setenv("LLM_ENABLE_THINKING", "")
    assert LLMConfig.from_env().enable_thinking is None
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    assert LLMConfig.from_env().enable_thinking is False
    monkeypatch.delenv("LLM_ENABLE_THINKING")
    assert LLMConfig.from_env().enable_thinking is False


def test_default_max_tokens_is_not_4096():
    """M1 踩过的坑：4096 会把条目密集的输出截断，整块作废。"""
    assert LLMConfig.from_env().max_tokens >= 8192


def test_stays_in_sync_with_m1_copy():
    """这份客户端是 M1 的副本，核心实现必须逐行一致，防止单边漂移。"""
    if not M1_CLIENT.exists():
        import pytest

        pytest.skip("仓库里没有 M1_Extraction，跳过一致性比对")

    def core(path):
        lines = path.read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("from __future__"))
        return "\n".join(lines[start:])

    assert core(Path(llm_client.__file__)) == core(M1_CLIENT), (
        "M2 的 llm_client.py 与 M1 的实现已经不一致，请同步两边"
    )
