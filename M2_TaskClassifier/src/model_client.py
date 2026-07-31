"""M2 使用的 OpenAI 兼容模型客户端与安全配置加载。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import yaml
from openai import OpenAI


class ModelClient(Protocol):
    def generate(self, system_prompt: str, user_prompt: str) -> dict:
        """返回 content、model 与 usage，不返回客户端密钥。"""


def _environment_override(config: dict, key: str, env_key: str) -> None:
    env_name = config.get(env_key)
    if env_name and os.getenv(env_name):
        config[key] = os.environ[env_name]


def resolve_llm_config(llm_config: dict) -> dict:
    """合并 M1 既有模型配置和 M2 非敏感覆盖项。"""
    resolved: dict = {}
    inherit = llm_config.get("inherit") or {}
    if inherit:
        source_path = Path(inherit["config_path"])
        source = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
        provider = inherit["provider"]
        resolved.update(source.get("llm", {}).get(provider, {}))

    for key in (
        "api_key",
        "base_url",
        "model",
        "timeout_seconds",
        "max_retries",
        "max_tokens",
        "structured_output",
        "temperature",
    ):
        if key in llm_config and llm_config[key] is not None:
            resolved[key] = llm_config[key]

    _environment_override(llm_config, "api_key", "api_key_env")
    _environment_override(llm_config, "base_url", "base_url_env")
    _environment_override(llm_config, "model", "model_env")
    for key in ("api_key", "base_url", "model"):
        if key in llm_config and llm_config[key]:
            resolved[key] = llm_config[key]

    missing = [key for key in ("api_key", "base_url", "model") if not resolved.get(key)]
    if missing:
        raise ValueError(f"M2 模型配置缺少字段：{', '.join(missing)}")
    return resolved


class OpenAICompatibleClient:
    """确定性调用一个 OpenAI Chat Completions 兼容端点。"""

    def __init__(self, config: dict) -> None:
        resolved = resolve_llm_config(config)
        self.client = OpenAI(
            api_key=resolved["api_key"],
            base_url=resolved["base_url"],
            timeout=resolved.get("timeout_seconds", 90),
            max_retries=resolved.get("max_retries", 2),
        )
        self.model = resolved["model"]
        self.max_tokens = int(resolved.get("max_tokens", 512))
        self.temperature = float(resolved.get("temperature", 0.0))
        self.structured_output = bool(resolved.get("structured_output", True))

    def generate(self, system_prompt: str, user_prompt: str) -> dict:
        request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.structured_output:
            request["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**request)
        usage = response.usage
        return {
            "content": response.choices[0].message.content or "",
            "model": response.model or self.model,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        }
