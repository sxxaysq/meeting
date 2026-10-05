# -*- coding: utf-8 -*-
"""M4 招投标分类器：LLM 主判 + 正则交叉校验（不改判）。

识别主体是 LLM（复用 M1_Extraction/src/llm_client.py，只读 import），
温度 0、禁思考，与 M1 传统一致。正则只做补充校验：强关键词命中但 LLM 判为
非招投标的条目记入 disagreements 清单进报告，供人工抽查；不自动翻转判定。

错误传统与 M1 一致：LLM 输出非法（JSON 不可解析、idx 缺失/重复、类目越界、
布尔不一致）即抛 ClassificationError，不静默修正、不吞异常。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
M1_ROOT = HERE.parents[1] / "M1_Extraction"
sys.path.insert(0, str(M1_ROOT / "src"))

from llm_client import LLMClient  # noqa: E402

PROMPT_PATH = HERE / "prompts" / "bidding_classifier.md"

# 招投标类目：非招投标之外的四类
BIDDING_CATEGORIES = ("TENDER", "BID", "OPEN_EVALUATION", "AWARD_CONTRACT")
ALL_CATEGORIES = BIDDING_CATEGORIES + ("NON_BIDDING",)

# 正则交叉校验用的强关键词（仅校验，不参与判定）
STRONG_KEYWORD_PATTERN = re.compile(
    r"招标文件|招标|投标|开标|评标|定标|中标|标书|废标|流标|招采|招投标"
)

# 单条送入 LLM 的字段截断上限（控制 prompt 体量；generic 条目通常远短于此）
_CONTENT_MAX_CHARS = 300
_EVIDENCE_MAX_CHARS = 200
# 分批大小：控制单次输出的 JSON 体量，避免长输出截断
DEFAULT_CHUNK_SIZE = 80


class ClassificationError(RuntimeError):
    """LLM 分类结果非法。不静默修正，由调用方决定失败路径。"""


def load_system_prompt() -> str:
    """从 prompts/bidding_classifier.md 提取 system 段
    （含判定规则、输出契约与 few-shot 示例，到用户提示模板为止）。"""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    start = text.index("## 系统提示（system）") + len("## 系统提示（system）")
    end = text.index("## 用户提示（user）模板")
    return text[start:end].strip()


def _render_item_block(idx: int, item: dict) -> str:
    evidence_text = item.get("evidence", {}).get("text", "")
    return (
        "[idx={idx}] item_type={item_type}\n"
        "标题：{title}\n"
        "内容：{content}\n"
        "证据：{evidence}".format(
            idx=idx,
            item_type=item.get("item_type", ""),
            title=item.get("title", ""),
            content=item.get("content", "")[:_CONTENT_MAX_CHARS],
            evidence=evidence_text[:_EVIDENCE_MAX_CHARS],
        )
    )


def build_user_prompt(items: List[dict], indices: List[int]) -> str:
    blocks = [_render_item_block(idx, items[idx]) for idx in indices]
    return "会议条目共 {} 条，逐条判断是否招投标类：\n\n{}".format(
        len(blocks), "\n\n".join(blocks)
    )


def _validate_results(
    raw: Optional[dict], indices: List[int]
) -> Dict[int, dict]:
    """程序性校验 LLM 输出：idx 全覆盖且不重复、类目合法、布尔一致。非法即抛错。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
        raise ClassificationError("LLM 输出缺少 results 数组")
    results: Dict[int, dict] = {}
    for entry in raw["results"]:
        if not isinstance(entry, dict):
            raise ClassificationError("results 中出现非对象元素：{}".format(entry))
        idx = entry.get("idx")
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise ClassificationError("idx 不是整数：{}".format(entry))
        if idx not in indices:
            raise ClassificationError("idx={} 超出本批范围 {}".format(idx, indices))
        if idx in results:
            raise ClassificationError("idx={} 重复出现".format(idx))
        is_bidding = entry.get("is_bidding")
        category = entry.get("category")
        reason = entry.get("reason")
        if not isinstance(is_bidding, bool):
            raise ClassificationError("idx={} 的 is_bidding 不是布尔值".format(idx))
        if not isinstance(category, str) or category not in ALL_CATEGORIES:
            raise ClassificationError(
                "idx={} 的 category 非法：{}（合法值：{}）".format(idx, category, ALL_CATEGORIES)
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ClassificationError("idx={} 的 reason 缺失或为空".format(idx))
        expected = "NON_BIDDING" if not is_bidding else None
        if not is_bidding and category != expected:
            raise ClassificationError(
                "idx={} 的 is_bidding=false 但 category={}".format(idx, category)
            )
        if is_bidding and category not in BIDDING_CATEGORIES:
            raise ClassificationError(
                "idx={} 的 is_bidding=true 但 category={}".format(idx, category)
            )
        results[idx] = {
            "idx": idx,
            "is_bidding": is_bidding,
            "category": category,
            "reason": reason.strip(),
        }
    missing = [idx for idx in indices if idx not in results]
    if missing:
        raise ClassificationError("LLM 输出缺少 idx：{}".format(missing[:20]))
    return results


def regex_hits(item: dict) -> bool:
    """正则补充校验：title/content/evidence 是否命中招投标强关键词。"""
    text = " ".join(
        (
            item.get("title", "") or "",
            item.get("content", "") or "",
            item.get("evidence", {}).get("text", "") or "",
        )
    )
    return bool(STRONG_KEYWORD_PATTERN.search(text))


def cross_check(
    items: List[dict], annotations: Dict[int, dict]
) -> List[dict]:
    """正则交叉校验：只记录分歧，不改判。分歧以 LLM 判定为准。"""
    disagreements = []
    for idx, annotation in sorted(annotations.items()):
        item = items[idx]
        hit = regex_hits(item)
        if annotation["is_bidding"] and not hit:
            disagreements.append(
                {
                    "idx": idx,
                    "kind": "llm_yes_no_keyword",
                    "title": item.get("title", ""),
                    "category": annotation["category"],
                    "reason": annotation["reason"],
                }
            )
        elif not annotation["is_bidding"] and hit:
            disagreements.append(
                {
                    "idx": idx,
                    "kind": "regex_hit_llm_no",
                    "title": item.get("title", ""),
                    "category": annotation["category"],
                    "reason": annotation["reason"],
                }
            )
    return disagreements


def classify_items(
    items: List[dict],
    client: LLMClient,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict:
    """对一批 m1 条目做招投标分类（分批调用 LLM，每批一次调用）。

    返回：
        annotations: {idx: {"idx", "is_bidding", "category", "reason"}}（全部条目）
        bidding_indices: [idx, ...]（is_bidding=true，升序）
        llm_stats: client.stats() 快照
    非法输出直接抛 ClassificationError（不静默修正）。
    """
    system = load_system_prompt()
    annotations: Dict[int, dict] = {}
    for start in range(0, len(items), chunk_size):
        indices = list(range(start, min(start + chunk_size, len(items))))
        user = build_user_prompt(items, indices)
        # 唯一一次 repair（与 M1 传统一致）：输出不符合契约时回灌重问，
        # 仍非法则抛错，不静默修正。
        try:
            raw = client.complete_json(system, user)
            if raw is None:
                raise ClassificationError("输出不可解析")
            annotations.update(_validate_results(raw, indices))
        except ClassificationError:
            repair_user = (
                user
                + "\n\n上一次输出不符合契约。只输出一个 JSON 对象："
                "{\"results\": [{\"idx\": <下标>, \"is_bidding\": <布尔>, "
                "\"category\": <TENDER|BID|OPEN_EVALUATION|AWARD_CONTRACT|NON_BIDDING>, "
                "\"reason\": <依据>}, ...]}，每条输入恰好一个元素，不要任何解释。"
            )
            raw = client.complete_json(system, repair_user)
            if raw is None:
                raise ClassificationError(
                    "LLM 分类输出不可解析（items[{}:{}]）".format(indices[0], indices[-1])
                )
            annotations.update(_validate_results(raw, indices))
    bidding_indices = sorted(idx for idx, a in annotations.items() if a["is_bidding"])
    return {
        "annotations": annotations,
        "bidding_indices": bidding_indices,
        "llm_stats": client.stats(),
    }


def split_items(
    items: List[dict], annotations: Dict[int, dict]
) -> dict:
    """按分类结果切分：bidding 带注记，filtered 保持 m1 九字段原样。"""
    bidding = []
    for idx in sorted(idx for idx, a in annotations.items() if a["is_bidding"]):
        annotation = annotations[idx]
        bidding.append(
            {
                "idx": idx,
                "bidding_category": annotation["category"],
                "bidding_reason": annotation["reason"],
                "item": items[idx],
            }
        )
    filtered = [item for idx, item in enumerate(items) if not annotations[idx]["is_bidding"]]
    return {"bidding": bidding, "filtered": filtered}
