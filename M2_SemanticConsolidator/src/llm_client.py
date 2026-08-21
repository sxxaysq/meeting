# -*- coding: utf-8 -*-
"""OpenAI 兼容的模型客户端。

**与 ``M1_Extraction/src/llm_client.py`` 是同一份实现的副本。**
仓库里每个模块自带 ``src/`` 与 ``requirements.txt``，跨模块 import 需要
sys.path 拼接，反而更脆；所以这里按既有约定复制一份。
``tests/test_llm_client.py`` 会逐字节比对两份文件的核心实现，
一边改了另一边没跟上会直接测试失败，不会静默漂移。

模型名不写死在业务逻辑里，全部走环境变量：
    LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_TIMEOUT

关于 LLM_TRANSPORT
------------------
默认 ``openai``，即标准 ``/v1/chat/completions``。

本机 Ollama 的 OpenAI 兼容层会把上下文硬截断到 4096 token（实测：
9000 token 的输入 prompt_tokens 仍然只有 4096），长 Block 会被悄悄截掉。
Ollama 原生 ``/api/chat`` 支持 ``options.num_ctx``，所以额外提供
``LLM_TRANSPORT=ollama``，只改传输层，不改任何业务逻辑与提示词。
换成 vLLM / 云端模型时保持默认 ``openai`` 即可。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(RuntimeError):
    pass


def _parse_optional_bool(value: Optional[str]) -> Optional[bool]:
    """空字符串表示"根本不要发这个参数"，供不认识该参数的服务端使用。"""
    if value is None:
        return False
    value = value.strip().lower()
    if not value or value == "omit":
        return None
    return value in ("1", "true", "yes", "on")


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float
    transport: str
    num_ctx: Optional[int]
    temperature: float
    max_tokens: int
    enable_thinking: Optional[bool]
    extra_body: dict

    @classmethod
    def from_env(cls) -> "LLMConfig":
        num_ctx = os.getenv("LLM_NUM_CTX")
        try:
            extra_body = json.loads(os.getenv("LLM_EXTRA_BODY", "") or "{}")
        except json.JSONDecodeError as error:
            raise LLMError("LLM_EXTRA_BODY 不是合法 JSON：{}".format(error))
        return cls(
            base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:7060/v1").rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", "EMPTY"),
            model=os.getenv("LLM_MODEL", "qwen3:30b"),
            timeout=float(os.getenv("LLM_TIMEOUT", "600")),
            transport=os.getenv("LLM_TRANSPORT", "openai").lower(),
            num_ctx=int(num_ctx) if num_ctx else None,
            temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
            # 条目密集的 Block 一次能产出 25+ 个 Item，4096 会把 JSON 截断，
            # 整块结果作废。实测 1200 字符的 Block 需要 ~6000 token 输出。
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
            enable_thinking=_parse_optional_bool(os.getenv("LLM_ENABLE_THINKING")),
            extra_body=extra_body if isinstance(extra_body, dict) else {},
        )


def _post(url: str, payload: dict, timeout: float, api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key and api_key != "EMPTY":
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(
        url, json.dumps(payload).encode("utf-8"), headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_json(content: str) -> Optional[dict]:
    """从模型输出里取出 JSON 对象，容忍 think 段、``` 包裹和前后说明文字。"""
    if not content:
        return None
    content = THINK_BLOCK.sub("", content).strip()
    fenced = JSON_BLOCK.search(content)
    if fenced:
        content = fenced.group(1).strip()
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(content[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


class LLMClient:
    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()
        self.calls = 0
        self.repairs = 0
        self.failures = 0
        self.truncations = 0
        self.reasoning_fallbacks = 0

    # ---- 传输层 ---------------------------------------------------
    def _chat_openai(self, system: str, user: str) -> str:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self.config.enable_thinking is not None:
            # 思考型模型（Qwen3.x 等）默认会把推理写进独立字段，content 直接是 null。
            # 关掉思考既省 token 又让 JSON 直接落在 content 上。
            payload["chat_template_kwargs"] = {
                "enable_thinking": self.config.enable_thinking
            }
        payload.update(self.config.extra_body)
        data = _post(
            self.config.base_url + "/chat/completions",
            payload,
            self.config.timeout,
            self.config.api_key,
        )
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            self.truncations += 1
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if content.strip():
            return content
        # 兜底：模型把内容全写进了 reasoning，尝试从中取 JSON，并如实计数
        reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
        if reasoning.strip():
            self.reasoning_fallbacks += 1
        return reasoning

    def _chat_ollama(self, system: str, user: str) -> str:
        root = self.config.base_url
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        options = {
            "temperature": self.config.temperature,
            "num_predict": self.config.max_tokens,
        }
        if self.config.num_ctx:
            options["num_ctx"] = self.config.num_ctx
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": False,
            "format": "json",
            "options": options,
        }
        data = _post(root + "/api/chat", payload, self.config.timeout, self.config.api_key)
        return data.get("message", {}).get("content") or ""

    def _chat(self, system: str, user: str) -> str:
        transport = self.config.transport
        if transport == "ollama":
            return self._chat_ollama(system, user)
        return self._chat_openai(system, user)

    # ---- 对外接口 -------------------------------------------------
    def complete_json(self, system: str, user: str, retries: int = 2) -> Optional[dict]:
        """返回解析后的 JSON 对象；最多一次 repair，失败返回 None（不无限重试）。"""
        last_error = None
        content = ""
        for attempt in range(retries):
            try:
                self.calls += 1
                content = self._chat(system, user)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
                last_error = error
                if attempt == retries - 1:
                    break
                time.sleep(2 ** attempt)
                continue
            parsed = extract_json(content)
            if parsed is not None:
                return parsed
            if attempt == retries - 1:
                break
            # 唯一一次 repair：把失败输出回灌，要求只返回 JSON
            self.repairs += 1
            user = (
                user
                + "\n\n上一次输出不是合法 JSON 对象，请只输出 JSON，不要任何解释或 Markdown。"
            )
        self.failures += 1
        if last_error is not None:
            raise LLMError("模型调用失败：{}".format(last_error))
        return None

    def stats(self) -> dict:
        return {
            "model": self.config.model,
            "base_url": self.config.base_url,
            "transport": self.config.transport,
            "calls": self.calls,
            "repairs": self.repairs,
            "failures": self.failures,
            "truncations": self.truncations,
            "reasoning_fallbacks": self.reasoning_fallbacks,
        }
