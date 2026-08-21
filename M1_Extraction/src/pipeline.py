# -*- coding: utf-8 -*-
"""M1 主链路。

PDF/DOCX → 文本规范化(+page/char) → 结构 Block → 逐 Block Item 抽取
→ Flatten → Evidence 对齐 → 基础质量门 → Schema 校验 → items[]

最终产物只有 `{"items": [...]}`；问题、统计、被丢弃的候选放在单独的
report 里，不污染业务输出。
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from document_reader import read_document
from generic_extractor import extract_document_generic, whole_document_block
from item_extractor import BlockResult, extract_block, load_prompt
from llm_client import LLMClient
from quality_gate import Issue, check_items, summarize
from schema_validator import validate_items
from structure_segmenter import Block, blocks_to_payload, segment_blocks
from text_normalizer import NormalizedDocument, normalize


@dataclass
class PipelineResult:
    source_name: str
    mode: str = "block"
    items: List[dict] = field(default_factory=list)
    blocks: List[Block] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    dropped: List[dict] = field(default_factory=list)
    normalize_warnings: List[dict] = field(default_factory=list)
    block_errors: List[dict] = field(default_factory=list)
    schema_errors: List[str] = field(default_factory=list)
    llm_stats: dict = field(default_factory=dict)
    document: Optional[NormalizedDocument] = None
    raw_task_count: int = 0

    def items_payload(self) -> dict:
        return {"items": self.items}

    def report(self) -> dict:
        return {
            "source_name": self.source_name,
            "mode": self.mode,
            "block_count": len(self.blocks),
            "raw_task_count": self.raw_task_count,
            "item_count": len(self.items),
            "exact_match_rate": round(
                sum(1 for i in self.items if i["evidence"]["exact_match"])
                / max(1, len(self.items)),
                4,
            ),
            "item_type_distribution": _distribution(self.items, "item_type"),
            "llm": self.llm_stats,
            "quality_summary": summarize(self.issues),
            "schema_errors": self.schema_errors,
            "issues": [i.to_dict() for i in self.issues],
            "dropped_candidates": self.dropped,
            "normalize_warnings": self.normalize_warnings,
            "block_errors": self.block_errors,
        }


def _distribution(items: List[dict], field_name: str) -> dict:
    counts: dict = {}
    for item in items:
        key = item.get(field_name)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def run_pipeline(
    input_path,
    client: Optional[LLMClient] = None,
    max_block_chars: int = 1200,
    workers: int = 1,
    mode: str = "block",
    progress=None,
) -> PipelineResult:
    raw = read_document(input_path)
    doc = normalize(raw)
    if mode not in ("block", "generic"):
        raise RuntimeError("未知抽取模式：{}".format(mode))

    blocks = (
        [whole_document_block(doc)]
        if mode == "generic"
        else segment_blocks(doc, max_block_chars=max_block_chars)
    )

    result = PipelineResult(
        source_name=doc.source_name, mode=mode, blocks=blocks, document=doc
    )
    if client is None:
        return result

    if mode == "generic":
        generic = extract_document_generic(client, doc)
        result.items.extend(generic.items)
        result.dropped.extend(generic.dropped)
        result.normalize_warnings.extend(generic.warnings)
        result.raw_task_count = generic.raw_task_count
        if generic.error:
            result.block_errors.append({"block_index": 0, "error": generic.error})
        if progress:
            progress(1, 1)
        result.llm_stats = client.stats()
        result.issues = check_items(result.items_payload(), doc)
        result.schema_errors = validate_items(result.items_payload(), strict=False)
        return result

    template = load_prompt()
    total = len(blocks)

    def work(block: Block) -> BlockResult:
        return extract_block(client, block, doc, template)

    block_results: List[BlockResult] = [None] * total  # type: ignore[list-item]
    if workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, block): block.index for block in blocks}
            done = 0
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                block_results[index] = future.result()
                done += 1
                if progress:
                    progress(done, total)
    else:
        for position, block in enumerate(blocks, start=1):
            block_results[block.index] = work(block)
            if progress:
                progress(position, total)

    for block_result in block_results:
        if block_result is None:
            continue
        result.items.extend(block_result.items)
        result.dropped.extend(block_result.dropped)
        result.normalize_warnings.extend(block_result.warnings)
        if block_result.error:
            result.block_errors.append(
                {"block_index": block_result.block_index, "error": block_result.error}
            )

    result.raw_task_count = len(result.items) + len(result.dropped)
    result.llm_stats = client.stats()
    result.issues = check_items(result.items_payload(), doc)
    # 这里不抛异常：先把结果和诊断都装进 result，由调用方决定是否落盘/失败。
    # 否则 Schema 一失败，连排查用的 report 都拿不到。
    result.schema_errors = validate_items(result.items_payload(), strict=False)
    return result


def blocks_only(input_path, max_block_chars: int = 1200) -> dict:
    doc = normalize(read_document(input_path))
    return blocks_to_payload(segment_blocks(doc, max_block_chars=max_block_chars))
