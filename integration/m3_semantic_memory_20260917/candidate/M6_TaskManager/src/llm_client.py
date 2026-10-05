"""OpenAI-compatible JSON client without persisting hidden reasoning."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol


def parse_json_object(content: str) -> dict[str, Any]:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("生命周期模型输出必须是 JSON 对象")
    return parsed


class LifecycleClient(Protocol):
    def decide(
        self,
        system_prompt: str,
        payload: dict[str, Any],
        correction: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return a decision and a reasoning-free usage audit."""


class OpenAICompatibleLifecycleClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        timeout_seconds: float = 180,
        enable_thinking: bool | None = None,
    ) -> None:
        from openai import OpenAI

        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key or "EMPTY",
            timeout=timeout_seconds,
            max_retries=0,  # The service owns one bounded transport retry.
        )
        self.model = model
        if enable_thinking is None:
            enable_thinking = os.getenv(
                "LLM_ENABLE_THINKING", "false"
            ).strip().lower() in {"1", "true", "yes", "on"}
        self.enable_thinking = enable_thinking

    def decide(
        self,
        system_prompt: str,
        payload: dict[str, Any],
        correction: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        user_content = json.dumps(payload, ensure_ascii=False, indent=2)
        if correction:
            user_content += (
                "\n\n上一次 JSON 未通过确定性格式校验。"
                "请只修复以下问题并重新输出完整 JSON：\n"
                + correction
            )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
            max_tokens=1200,
            response_format={"type": "json_object"},
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": self.enable_thinking,
                }
            },
        )
        content = response.choices[0].message.content or ""
        usage = response.usage
        audit = {
            "model": response.model or self.model,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        }
        return parse_json_object(content), audit


class ScriptedLifecycleClient:
    """Deterministic client for tests and reproducible evaluator fixtures."""

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)

    def decide(
        self,
        system_prompt: str,
        payload: dict[str, Any],
        correction: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del system_prompt, payload, correction
        if not self.decisions:
            raise RuntimeError("脚本化生命周期决策已耗尽")
        return self.decisions.pop(0), {"model": "scripted", "usage": {}}
