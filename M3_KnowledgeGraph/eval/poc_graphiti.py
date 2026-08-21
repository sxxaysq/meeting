#!/usr/bin/env python3
"""M3 Graphiti POC：验证 Graphiti + 本地 Neo4j + 本地 Qwen(OpenAI-compatible) 的兼容性。

结论导向：
1. Graphiti 官方 OpenAIClient 走 Responses API 做结构化输出，vLLM 不支持 → 子类化改写为
   chat.completions + response_format。
2. Qwen3.x 必须注入 chat_template_kwargs.enable_thinking=false，否则 content 为 null。
3. vLLM 无 embeddings 接口 → POC 用确定性 hash embedder（可替换）。

用法：
    /home/yty/m1x_venv/bin/python M3_KnowledgeGraph/eval/poc_graphiti.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os

os.environ.setdefault("POSTHOG_DISABLED", "1")

import typing
from datetime import datetime, timezone

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.nodes import EpisodeType
from neo4j import GraphDatabase

NEO4J_URI = os.environ.get("M3_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("M3_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("M3_NEO4J_PASSWORD", "m3graph2026")
LLM_BASE_URL = os.environ.get("M3_LLM_BASE_URL", "http://192.168.30.215:8000/v1")
LLM_MODEL = os.environ.get("M3_LLM_MODEL", "Qwen/Qwen3.6-35B-A3B")

EMBEDDING_DIM = 1024  # 与 graphiti_core 默认 EMBEDDING_DIM 对齐（POC hash 向量）


class QwenOpenAIClient(OpenAIClient):
    """适配本地 vLLM/Qwen 的 Graphiti LLM 客户端。

    - 结构化输出改走 chat.completions + response_format（vLLM 无 Responses API）。
    - 所有请求注入 enable_thinking=false（Qwen3.x 思考模式会吞掉 content）。
    """

    NO_THINK_EXTRA = {"chat_template_kwargs": {"enable_thinking": False}}

    async def _create_structured_completion(
        self,
        model: str,
        messages,
        temperature: float | None,
        max_tokens: int,
        response_model,
        reasoning: str | None = None,
        verbosity: str | None = None,
    ):
        kwargs: dict[str, typing.Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "extra_body": self.NO_THINK_EXTRA,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        try:
            schema = response_model.model_json_schema()
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": schema,
                },
            }
            response = await self.client.chat.completions.create(**kwargs)
        except Exception:
            # vLLM guided decoding 若拒绝 schema，退回 json_object
            kwargs["response_format"] = {"type": "json_object"}
            response = await self.client.chat.completions.create(**kwargs)
        return _ChatWrapper(response)

    async def _create_completion(
        self,
        model: str,
        messages,
        temperature: float | None,
        max_tokens: int,
        response_model=None,
        reasoning: str | None = None,
        verbosity: str | None = None,
    ):
        kwargs: dict[str, typing.Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "extra_body": self.NO_THINK_EXTRA,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        return await self.client.chat.completions.create(**kwargs)


class _ChatWrapper:
    """把 chat.completions 响应伪装成 responses.parse 的形态（供 _handle_structured_response）。"""

    def __init__(self, chat_response):
        self._r = chat_response

    @property
    def output_text(self):
        return self._r.choices[0].message.content

    @property
    def usage(self):
        u = self._r.usage
        if u is None:
            return None

        class _U:
            input_tokens = u.prompt_tokens
            output_tokens = u.completion_tokens

        return _U()

    refusal = None


class HashEmbedder(EmbedderClient):
    """确定性 hash 向量 embedder（POC/离线兜底）。

    vLLM 未提供 embeddings 接口；第一版 M3 的检索以 Cypher 精确查询为主，
    向量仅用于满足 Graphiti schema。后续可换成真实 embedding 模型。
    """

    async def create(self, input_data):
        text = input_data if isinstance(input_data, str) else json.dumps(
            input_data, ensure_ascii=False, default=str
        )
        vec = [0.0] * EMBEDDING_DIM
        # char 2-gram hashing
        grams = [text[i : i + 2] for i in range(max(len(text) - 1, 1))] or [text]
        for g in grams:
            h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "big")
            idx = h % EMBEDDING_DIM
            sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def create_batch(self, input_data_list):
        return [await self.create(x) for x in input_data_list]


async def main() -> None:
    print(f"[poc] neo4j={NEO4J_URI} model={LLM_MODEL}")
    llm = QwenOpenAIClient(config=_cfg())
    graphiti = Graphiti(
        NEO4J_URI,
        NEO4J_USER,
        NEO4J_PASSWORD,
        llm_client=llm,
        embedder=HashEmbedder(),
        cross_encoder=OpenAIRerankerClient(config=_cfg(), client=llm),
    )
    await graphiti.build_indices_and_constraints()
    print("[poc] build_indices_and_constraints OK")

    ref_time = datetime(2026, 4, 13, tzinfo=timezone.utc)
    payload = {
        "source_mode": "generic",
        "meeting_date": "2026-04-13",
        "source_document": "2026.4.13信息公司周例会工作安排备忘录.pdf",
        "items_sample": [
            {
                "department": "智能矿山事业部",
                "project": "红沙泉二矿项目",
                "title": "推进红沙泉二矿智能化建设方案评审",
                "item_type": "PROJECT_TASK",
            }
        ],
    }
    results = await graphiti.add_episode(
        name="poc-episode-2026-04-13",
        episode_body=json.dumps(payload, ensure_ascii=False),
        source_description="M3 Graphiti POC: M2 structured JSON episode",
        reference_time=ref_time,
        source=EpisodeType.json,
        group_id="m3-poc",
    )
    print("[poc] add_episode OK; episode:", results.episode.uuid)
    print("[poc] extracted entity nodes:", [n.name for n in results.nodes][:10])
    print(
        "[poc] extracted edges:",
        [(e.source_node_uuid[:8], e.name, e.target_node_uuid[:8]) for e in results.edges][:10],
    )

    # 直接用 neo4j driver 验证落库
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with drv.session() as s:
        n = s.run("MATCH (n) WHERE n.group_id='m3-poc' RETURN labels(n)[0] AS l, count(*) AS c").data()
        print("[poc] neo4j node counts:", n)
    drv.close()
    await graphiti.close()
    print("[poc] DONE")


def _cfg():
    from graphiti_core.llm_client.client import LLMConfig

    return LLMConfig(api_key="EMPTY", model=LLM_MODEL, base_url=LLM_BASE_URL, small_model=LLM_MODEL)


if __name__ == "__main__":
    asyncio.run(main())
