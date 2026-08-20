"""OpenAI 兼容 M6 客户端与测试用确定性客户端。"""

from __future__ import annotations

import json
import re
from typing import Protocol

from .prompt_builder import SYSTEM_PROMPT, build_user_prompt


class M6Client(Protocol):
    def generate(
        self,
        meeting: dict,
        source: dict,
        match_context: dict,
        correction: str | None = None,
    ) -> tuple[dict, dict]:
        """返回命令批次与不含密钥的审计摘要。"""


def _parse_json_object(content: str) -> dict:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("M6 响应必须是 JSON 对象")
    return parsed


class OpenAICompatibleM6Client:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        timeout_seconds: float = 120,
    ) -> None:
        from openai import OpenAI

        self.client = OpenAI(
            api_key=api_key or "EMPTY",
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=1,
        )
        self.model = model

    def generate(
        self,
        meeting: dict,
        source: dict,
        match_context: dict,
        correction: str | None = None,
    ) -> tuple[dict, dict]:
        user_prompt = build_user_prompt(meeting, source, match_context)
        if correction:
            user_prompt += (
                "\n\n上一次输出未通过确定性校验。请修复以下错误后重新输出完整 JSON："
                + correction
            )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=1800,
            response_format={"type": "json_object"},
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": False,
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
            "response_excerpt": content[:1000],
        }
        return _parse_json_object(content), audit


def empty_patch(**overrides: object) -> dict:
    patch = {
        "title": None,
        "description": None,
        "work_items": None,
        "assignee_raw": None,
        "deadline_raw": None,
        "status": None,
    }
    patch.update(overrides)
    return patch


class RuleBasedM6Client:
    """只用于测试和离线 smoke，不作为生产语义模型。"""

    def generate(
        self,
        meeting: dict,
        source: dict,
        match_context: dict,
        correction: str | None = None,
    ) -> tuple[dict, dict]:
        del correction
        text = source["text"]
        source_key = (
            f"{meeting['meeting_id']}:{source['segment_id']}:"
            f"{source['subsegment_id']}"
        )
        action = "NOOP"
        patch = empty_patch()
        target = None
        version = None
        reason = "NOT_ACTIONABLE"
        unique_target = match_context.get("unique_target_task_id")
        candidate = next(
            (
                item
                for item in match_context.get("candidates", [])
                if item["task_id"] == unique_target
            ),
            None,
        )

        if source["task_category"] == "task":
            action = "CREATE"
            title = re.sub(r"[。！!]+$", "", text).strip()
            patch = empty_patch(
                title=title,
                description=title,
                assignee_raw=source.get("actor_hint"),
                status="open",
            )
            reason = "NEW_TASK"
        elif source["task_category"] == "task_update" and candidate:
            target = candidate["task_id"]
            version = candidate["version"]
            if any(word in text for word in ("取消", "不再执行", "删除")):
                action = "SOFT_DELETE"
                reason = "TASK_CANCELLED"
            elif any(word in text for word in ("完成", "已办结")):
                action = "UPDATE_STATUS"
                patch = empty_patch(status="completed")
                reason = "TASK_COMPLETED"
            elif any(word in text for word in ("受阻", "无法继续", "暂停")):
                action = "UPDATE_STATUS"
                patch = empty_patch(status="blocked")
                reason = "TASK_BLOCKED"
            else:
                action = "UPDATE_FIELDS"
                deadline = "月底" if "月底" in text else text
                patch = empty_patch(deadline_raw=deadline)
                reason = "TASK_FIELDS_CHANGED"
        elif source["task_category"] == "task_update":
            reason = "NOOP_UNMATCHED"

        batch = {
            "schema_version": "m6_db_command_v1",
            "source_key": source_key,
            "commands": [
                {
                    "command_index": 1,
                    "db_action": action,
                    "target_task_id": target,
                    "expected_version": version,
                    "task_patch": patch,
                    "evidence": {
                        "segment_id": source["segment_id"],
                        "subsegment_id": source["subsegment_id"],
                        "text": text,
                    },
                    "reason_code": reason,
                    "ambiguities": [],
                }
            ],
        }
        return batch, {"model": "rule-based-test-client", "usage": {}}
