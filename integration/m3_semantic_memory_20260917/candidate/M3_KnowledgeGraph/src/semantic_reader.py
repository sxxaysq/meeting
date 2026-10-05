"""LLM reading of evidence semantics, cached in the long-term memory graph.

Replaces the regex predicates that used to guard lifecycle operations in
``M6_TaskManager/src/command_validator.py``:

``已(?:经)?完成|已经?通过.{0,12}验收|验收通过|已交付|已结题|全部完成``
    → flag ``definite``
``计划|拟|下周|要求|确保|如果|假如|若|待完成|需完成|未…|没有…完成|\\d+%``
    → flag ``future_or_partial``
``项目整体|全部子系统|全部工作|整体验收``
    → flag ``whole_goal``
``装修|安装|研发|开发|测试|验收|施工|调试``
    → flag ``phases_covered``
``移交|转交|改由|改为|由…接手/负责/牵头``
    → flag ``explicit_transfer``
``不(?:得|应|要|再)?…|暂不|无需|禁止|避免|尚未``
    → flag ``negated``
``更名|名称.*改为|调整为|变更为``
    → flag ``explicit_rename``

A keyword list cannot tell ``不得取消`` from ``取消``, or ``完成30%`` from
``完成``; that is precisely why these were brittle. The reading is now one
focused model call per distinct (kind, evidence, task goal), and the answer is
written to ``M3MemSemantic`` so the same wording in a later meeting is free.

Nothing here writes to a task. It only reports what the evidence says.
"""

from __future__ import annotations

import json
from typing import Any

from .semantic_memory import SemanticMemory

COMPLETION_PROMPT = '''判断证据是否证明"整个业务目标已经完成"。只返回 JSON：
{"definite":布尔,"future_or_partial":布尔,"whole_goal":布尔,"phases_covered":布尔,"goal_in_evidence":布尔,"reason":"简短依据"}

definite：证据明示已经完成、已验收通过、已交付、已结题；不是"将要完成"。
future_or_partial：证据或其紧邻上文是计划、拟、要求、确保、下周、待完成、需完成，或含否定（未、没有、尚未、不得、暂不），或只是百分比进度而非全部完成。
whole_goal：证据指向整个目标的完成，而不是其中一个子事项、单个阶段或后续安排。
phases_covered：任务目标里列出的各个阶段（例如装修、安装、调试、验收）都被证据覆盖；只有一个阶段完成时为 false。目标本身不含多阶段时填 true。
goal_in_evidence：证据确实在讲这个任务的目标，而不是别的工作。
只依据给出的证据和上下文，不要推测未写明的内容。不确定时把 definite 填 false。'''

# Not ``str.format``: the prompt contains literal JSON braces, and ``format``
# would read ``{"explicit": ...}`` as a replacement field. The same trap is
# documented in M2's ``text_utils._ORDINAL``.
STATUS_CHANGE_PROMPT = '''判断证据是否明示了一次状态变更。只返回 JSON：
{"explicit":布尔,"negated":布尔,"reason":"简短依据"}

变更类型：__KIND_DESCRIPTION__
explicit：证据原文直接陈述了该变更已经发生或被决定。
negated：证据或其紧邻上文否定了该变更（不得、不应、暂不、无需、禁止、避免、尚未、不予）。
只依据给出的证据和上下文。不确定时把 explicit 填 false。'''

RENAME_PROMPT = '''判断证据是否明示了一次更名或字段调整。只返回 JSON：
{"explicit_rename":布尔,"reason":"简短依据"}

explicit_rename：证据原文直接说明名称改为、调整为、变更为某值，而不只是描述工作内容。
只依据给出的证据和上下文。不确定时填 false。'''

KIND_DESCRIPTIONS = {
    "CANCEL": "取消或终止该目标，不再开展",
    "REOPEN": "重新启动、重新开展、恢复开展该目标",
    "TRANSFER": "把该工作的责任移交给另一个部门（移交、转交、改由某部门接手/负责/牵头）",
}

COMPLETION_FLAGS = (
    "definite",
    "future_or_partial",
    "whole_goal",
    "phases_covered",
    "goal_in_evidence",
)
STATUS_FLAGS = ("explicit", "negated")
RENAME_FLAGS = ("explicit_rename",)


def _coerce_flags(raw: Any, expected: tuple[str, ...]) -> dict[str, bool] | None:
    if not isinstance(raw, dict):
        return None
    flags: dict[str, bool] = {}
    for name in expected:
        value = raw.get(name)
        if not isinstance(value, bool):
            return None
        flags[name] = value
    return flags


class SemanticReader:
    """Reads evidence semantics through the model, remembered by the graph."""

    def __init__(self, memory: SemanticMemory, client: Any) -> None:
        self.memory = memory
        self.client = client
        self.calls = 0
        self.cache_hits = 0
        self.format_retries = 0
        self.service_failures = 0
        self.unreadable = 0

    # ------------------------------------------------------------------ public

    def read_completion(
        self,
        *,
        evidence: str,
        context: str,
        task_title: str,
        task_description: str = "",
        task_project: str = "",
    ) -> dict[str, Any]:
        subject = "\u241f".join([task_title, task_project])
        cached = self._cached("COMPLETION", evidence, subject)
        if cached is not None:
            return cached
        payload = {
            "evidence": evidence,
            "evidence_context": context,
            "task_goal": task_title,
            "task_description": task_description[:400],
            "task_project": task_project,
        }
        raw = self._call(COMPLETION_PROMPT, payload)
        flags = _coerce_flags(raw, COMPLETION_FLAGS)
        if flags is None:
            self.unreadable += 1
            # Unreadable means "not proven", never "proven".
            flags = {name: False for name in COMPLETION_FLAGS}
            flags["future_or_partial"] = True
        result = {**flags, "reason": str((raw or {}).get("reason") or "")[:300], "cached": False}
        self._remember("COMPLETION", evidence, subject, flags, result["reason"])
        return result

    def read_status_change(
        self,
        kind: str,
        *,
        evidence: str,
        context: str,
        task_title: str = "",
    ) -> dict[str, Any]:
        if kind not in KIND_DESCRIPTIONS:
            raise ValueError(f"unknown status change kind {kind!r}")
        cached = self._cached(kind, evidence, task_title)
        if cached is not None:
            return cached
        prompt = STATUS_CHANGE_PROMPT.replace(
            "__KIND_DESCRIPTION__", KIND_DESCRIPTIONS[kind]
        )
        payload = {"kind": kind, "evidence": evidence, "evidence_context": context, "task_goal": task_title}
        raw = self._call(prompt, payload)
        flags = _coerce_flags(raw, STATUS_FLAGS)
        if flags is None:
            self.unreadable += 1
            flags = {"explicit": False, "negated": True}
        result = {**flags, "reason": str((raw or {}).get("reason") or "")[:300], "cached": False}
        self._remember(kind, evidence, task_title, flags, result["reason"])
        return result

    def read_rename(
        self, *, evidence: str, context: str, task_title: str = ""
    ) -> dict[str, Any]:
        cached = self._cached("RENAME", evidence, task_title)
        if cached is not None:
            return cached
        payload = {"evidence": evidence, "evidence_context": context, "task_goal": task_title}
        raw = self._call(RENAME_PROMPT, payload)
        flags = _coerce_flags(raw, RENAME_FLAGS)
        if flags is None:
            self.unreadable += 1
            flags = {"explicit_rename": False}
        result = {**flags, "reason": str((raw or {}).get("reason") or "")[:300], "cached": False}
        self._remember("RENAME", evidence, task_title, flags, result["reason"])
        return result

    def stats(self) -> dict[str, Any]:
        return {
            "model_calls": self.calls,
            "cache_hits": self.cache_hits,
            "format_retries": self.format_retries,
            "service_failures": self.service_failures,
            "unreadable": self.unreadable,
        }

    # ----------------------------------------------------------------- private

    def _cached(self, kind: str, evidence: str, subject: str) -> dict[str, Any] | None:
        flags = self.memory.semantic_flags(kind, evidence, subject)
        if flags is None:
            return None
        self.cache_hits += 1
        self.memory.bump_semantic_hit(kind, evidence, subject)
        return {**flags, "reason": "记忆库复用", "cached": True}

    def _remember(
        self, kind: str, evidence: str, subject: str, flags: dict[str, bool], reason: str
    ) -> None:
        try:
            self.memory.remember_semantic(kind, evidence, flags, subject=subject, reason=reason)
        except Exception:
            # A memory outage must not fail an otherwise valid operation; the
            # reading was already obtained and will simply be recomputed later.
            pass

    def _call(self, prompt: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        error: Exception | None = None
        for _attempt in range(2):
            self.calls += 1
            try:
                raw, _audit = self.client.decide(
                    prompt, payload, correction=str(error) if error else None
                )
                return raw if isinstance(raw, dict) else None
            except (ValueError, json.JSONDecodeError) as exc:
                error = exc
                self.format_retries += 1
            except Exception as exc:
                self.service_failures += 1
                error = exc
        return None
