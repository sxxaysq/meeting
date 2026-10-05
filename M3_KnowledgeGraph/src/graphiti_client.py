"""Graphiti 时间知识图谱层（可选、增量）。

定位（重要）：
- M2 JSON 是事实源；确定性图结构由 neo4j_store 直写 Cypher 保证。
- Graphiti 只承担：structured JSON episode 留存、时间语义、后续 hybrid retrieval。
- 本模块不产出 M3 的正式业务节点，绝不做第二遍实体抽取来覆盖 M2 结论。

兼容性处理（POC 已验证）：
1. vLLM 无 Responses API → 结构化输出改走 chat.completions + response_format；
2. Qwen3.x 必须注入 enable_thinking=false；
3. vLLM 无 embeddings 接口 → 默认用确定性 HashEmbedder（可替换真实模型）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import typing
from datetime import datetime, timezone

NEO4J_URI = os.environ.get("M3_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("M3_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("M3_NEO4J_PASSWORD", "YOUR_PASSWORD")
LLM_BASE_URL = os.environ.get("M3_LLM_BASE_URL", "http://192.168.30.215:8000/v1")
LLM_MODEL = os.environ.get("M3_LLM_MODEL", "Qwen/Qwen3.6-35B-A3B")
GRAPHITI_GROUP = os.environ.get("M3_GRAPHITI_GROUP", "m3")

EMBEDDING_DIM = 1024

_NO_THINK_EXTRA = {"chat_template_kwargs": {"enable_thinking": False}}


def _llm_config():
    from graphiti_core.llm_client.client import LLMConfig

    return LLMConfig(api_key="EMPTY", model=LLM_MODEL, base_url=LLM_BASE_URL, small_model=LLM_MODEL)


def _build_clients():
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.llm_client.openai_client import OpenAIClient

    class _QwenClient(OpenAIClient):
        async def _create_structured_completion(self, model, messages, temperature, max_tokens,
                                                response_model, reasoning=None, verbosity=None):
            kwargs: dict[str, typing.Any] = {
                "model": model, "messages": messages, "max_tokens": max_tokens,
                "extra_body": _NO_THINK_EXTRA,
            }
            if temperature is not None:
                kwargs["temperature"] = temperature
            try:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": response_model.__name__,
                                    "schema": response_model.model_json_schema()},
                }
                response = await self.client.chat.completions.create(**kwargs)
            except Exception:
                kwargs["response_format"] = {"type": "json_object"}
                response = await self.client.chat.completions.create(**kwargs)
            return _ChatWrapper(response)

        async def _create_completion(self, model, messages, temperature, max_tokens,
                                     response_model=None, reasoning=None, verbosity=None):
            kwargs: dict[str, typing.Any] = {
                "model": model, "messages": messages, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"}, "extra_body": _NO_THINK_EXTRA,
            }
            if temperature is not None:
                kwargs["temperature"] = temperature
            return await self.client.chat.completions.create(**kwargs)

    cfg = _llm_config()
    llm = _QwenClient(config=cfg)
    reranker = OpenAIRerankerClient(config=cfg, client=llm)
    return llm, reranker


class _ChatWrapper:
    """把 chat.completions 响应适配成 responses.parse 形态。"""

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


class HashEmbedder:
    """确定性 hash embedder（无外部依赖）。后续可替换为真实 embedding 服务。"""

    async def create(self, input_data):
        text = input_data if isinstance(input_data, str) else json.dumps(
            input_data, ensure_ascii=False, default=str
        )
        vec = [0.0] * EMBEDDING_DIM
        grams = [text[i:i + 2] for i in range(max(len(text) - 1, 1))] or [text]
        for g in grams:
            h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "big")
            vec[h % EMBEDDING_DIM] += 1.0 if (h >> 8) % 2 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def create_batch(self, input_data_list):
        return [await self.create(x) for x in input_data_list]


async def _record_episode_async(envelope: dict) -> dict:
    from graphiti_core import Graphiti
    from graphiti_core.nodes import EpisodeType

    llm, reranker = _build_clients()
    graphiti = Graphiti(
        NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD,
        llm_client=llm, embedder=HashEmbedder(), cross_encoder=reranker,
    )
    try:
        await graphiti.build_indices_and_constraints()
        m2 = envelope["m2_output"]
        meeting_date = datetime.strptime(envelope["meeting_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        doc_id = envelope["source_document_id"]
        payload = {
            "source_mode": m2["source_mode"],
            "meeting_date": envelope["meeting_date"],
            "source_document": doc_id,
            "validation_status": m2["validation"]["status"],
            "project_entities": [
                {"entity_id": pe["entity_id"], "canonical_name": pe["canonical_name"],
                 "aliases": pe.get("aliases", [])}
                for pe in m2["project_entities"]
            ],
            "items": [
                {"department": it.get("department"), "project": it.get("project"),
                 "item_type": it["item_type"], "title": it["title"],
                 "assignee": it.get("assignee", [])}
                for it in m2["items"]
            ],
        }
        results = await graphiti.add_episode(
            name=f"m2-{doc_id}",
            episode_body=json.dumps(payload, ensure_ascii=False),
            source_description="M2 SemanticConsolidator official output (structured JSON)",
            reference_time=meeting_date,
            source=EpisodeType.json,
            group_id=GRAPHITI_GROUP,
        )
        return {
            "episode_uuid": results.episode.uuid,
            "group_id": GRAPHITI_GROUP,
            "nodes_extracted": len(results.nodes),
            "edges_extracted": len(results.edges),
        }
    finally:
        await graphiti.close()


def record_episode(envelope: dict) -> dict:
    """同步入口：把一份已入图的 M2 信封同步记录为 Graphiti episode（时间/检索层）。"""
    return asyncio.run(_record_episode_async(envelope))
