# -*- coding: utf-8 -*-
"""LLM 客户端测试：思考型模型、请求体拼装、失败不无限重试。"""

import json

import pytest
from conftest import FIXTURES  # noqa: F401 - 同时把 src 注入 sys.path

import llm_client
from llm_client import LLMClient, LLMConfig, LLMError


def _config(**over):
    base = dict(
        base_url="http://x/v1",
        api_key="EMPTY",
        model="Qwen/Qwen3.6-35B-A3B",
        timeout=10,
        transport="openai",
        num_ctx=None,
        temperature=0,
        max_tokens=256,
        enable_thinking=False,
        extra_body={},
    )
    base.update(over)
    return LLMConfig(**base)


class _Capture:
    """替身 _post，记录请求体并返回预置响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def __call__(self, url, payload, timeout, api_key):
        self.payloads.append(payload)
        return self.responses.pop(0)


def _response(content=None, reasoning=None, finish="stop"):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {"content": content, "reasoning": reasoning},
            }
        ]
    }


@pytest.fixture
def patched(monkeypatch):
    def apply(responses):
        capture = _Capture(responses)
        monkeypatch.setattr(llm_client, "_post", capture)
        return capture

    return apply


# ---- 环境变量解析 ----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("false", False), ("0", False), ("true", True), ("1", True), ("", None), ("omit", None)],
)
def test_enable_thinking_parsing(monkeypatch, value, expected):
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    if value is not None:
        monkeypatch.setenv("LLM_ENABLE_THINKING", value)
    assert LLMConfig.from_env().enable_thinking is expected


def test_invalid_extra_body_raises(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY", "not json")
    with pytest.raises(LLMError):
        LLMConfig.from_env()


# ---- 请求体 ----------------------------------------------------------


def test_thinking_disabled_is_sent(patched):
    capture = patched([_response('{"items":[]}')])
    LLMClient(_config()).complete_json("sys", "user")
    assert capture.payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_thinking_omitted_when_none(patched):
    capture = patched([_response('{"items":[]}')])
    LLMClient(_config(enable_thinking=None)).complete_json("sys", "user")
    assert "chat_template_kwargs" not in capture.payloads[0]


def test_extra_body_merged(patched):
    capture = patched([_response('{"items":[]}')])
    LLMClient(_config(extra_body={"top_p": 0.8})).complete_json("sys", "user")
    assert capture.payloads[0]["top_p"] == 0.8


def test_response_format_is_json_object(patched):
    capture = patched([_response('{"items":[]}')])
    LLMClient(_config()).complete_json("sys", "user")
    assert capture.payloads[0]["response_format"] == {"type": "json_object"}


# ---- 思考型模型的返回 ------------------------------------------------


def test_content_is_used_when_present(patched):
    patched([_response('{"items":[1]}')])
    client = LLMClient(_config())
    assert client.complete_json("s", "u") == {"items": [1]}
    assert client.stats()["reasoning_fallbacks"] == 0


def test_falls_back_to_reasoning_when_content_null(patched):
    """content=null 是思考型模型的常见形态，不能当成失败。"""
    patched([_response(None, reasoning='思考…… {"items":[]}')])
    client = LLMClient(_config())
    assert client.complete_json("s", "u") == {"items": []}
    assert client.stats()["reasoning_fallbacks"] == 1


def test_truncation_is_counted(patched):
    patched([_response('{"items":[]}', finish="length")])
    client = LLMClient(_config())
    client.complete_json("s", "u")
    assert client.stats()["truncations"] == 1


# ---- 失败路径 --------------------------------------------------------


def test_one_repair_then_give_up(patched):
    capture = patched([_response("完全不是 JSON"), _response("还是不是 JSON")])
    client = LLMClient(_config())
    assert client.complete_json("s", "u") is None
    assert client.stats() == dict(
        client.stats(), calls=2, repairs=1, failures=1
    )
    assert "只输出 JSON" in capture.payloads[1]["messages"][1]["content"]


def test_repair_prompt_not_sent_on_success(patched):
    capture = patched([_response('{"items":[]}')])
    client = LLMClient(_config())
    client.complete_json("s", "u")
    assert len(capture.payloads) == 1
    assert client.stats()["repairs"] == 0


def test_stats_report_model_identity(patched):
    patched([_response('{"items":[]}')])
    client = LLMClient(_config())
    client.complete_json("s", "u")
    stats = client.stats()
    assert stats["model"] == "Qwen/Qwen3.6-35B-A3B"
    assert stats["transport"] == "openai"


def test_effective_max_tokens_clamped_by_context_budget():
    # prompt + max_tokens 不得超过 max_context_tokens，发送前按 prompt 字符数压缩
    client = LLMClient(_config(max_tokens=262144, max_context_tokens=10000))
    assert client._effective_max_tokens("", "x" * 8000) == 10000 - 8000 - 512


def test_effective_max_tokens_keeps_max_when_budget_large():
    client = LLMClient(_config(max_tokens=262144, max_context_tokens=300000))
    assert client._effective_max_tokens("s", "u") == 262144


def test_effective_max_tokens_has_floor():
    client = LLMClient(_config(max_tokens=262144, max_context_tokens=2000))
    assert client._effective_max_tokens("", "x" * 1900) == 1024


def test_payload_max_tokens_uses_clamped_value(patched):
    capture = patched([_response('{"items":[]}')])
    client = LLMClient(_config(max_tokens=262144, max_context_tokens=10000))
    client.complete_json("", "x" * 8000)
    assert capture.payloads[0]["max_tokens"] == 10000 - 8000 - 512
