# -*- coding: utf-8 -*-
"""中文项目名/标题的确定性文本工具。

这里的函数只负责**召回**与**硬否决**，不负责最终语义判定：
* 相似度用来生成候选，绝不用来直接决定 MERGE / SAME_ENTITY；
* 序数冲突（一矿/二矿、二期、2025/2026 年）是确定性硬否决，
  因为这类名字字面极近但业务上一定是不同项目，交给 LLM 反而容易错合。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional, Set

# 项目名里常见的通用尾缀。剥掉只用于放宽候选召回，不用于判等。
GENERIC_SUFFIXES = (
    "建设项目",
    "升级项目",
    "改造项目",
    "研发项目",
    "项目",
    "工程",
    "平台",
    "系统",
    "中心",
)

# 需要忽略的装饰符号
_PUNCT = re.compile(r"[\s　·・、，,。．.；;：:！!？?（）()【】\[\]《》<>\"'“”‘’—\-_/\\|]+")

_CJK_NUM = "〇零一二三四五六七八九十百千两"
# 序数/期次/年份 token：这些一旦不同，几乎必然是不同项目
# 不能用 .format 拼这个正则：相邻字面量会先拼成一个串，
# 里面的 \d{2} 会被当成占位符。直接用 + 拼接。
_ORDINAL = re.compile(
    r"(?:20\d{2}\s*年?)"
    r"|(?:[" + _CJK_NUM + r"\d]+\s*(?:期|矿|号|标段|阶段|批))"
)

_DIGITS = re.compile(r"\d+")
_CJK_DIGIT_MAP = {
    "〇": "0", "零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
}


def normalize_name(value: Optional[str]) -> str:
    """NFKC + 去标点空白 + 英文小写。用于 alias 的精确匹配键。"""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).strip()
    text = _PUNCT.sub("", text)
    return text.lower()


def core_name(value: Optional[str]) -> str:
    """剥掉通用尾缀后的核心名，只用于放宽候选召回。"""
    text = normalize_name(value)
    changed = True
    while changed and len(text) > 2:
        changed = False
        for suffix in GENERIC_SUFFIXES:
            stripped = normalize_name(suffix)
            if stripped and text.endswith(stripped) and len(text) - len(stripped) >= 2:
                text = text[: -len(stripped)]
                changed = True
                break
    return text


def _canonical_ordinal(token: str) -> str:
    """把 ``二期`` / ``2 期`` 归一成同一个可比较的 token。"""
    token = normalize_name(token)
    year = re.fullmatch(r"(20\d{2})年?", token)
    if year:
        return "Y" + year.group(1)
    digits = _DIGITS.search(token)
    if digits:
        number = digits.group(0).lstrip("0") or "0"
    else:
        number = "".join(_CJK_DIGIT_MAP.get(ch, "") for ch in token)
        number = number.lstrip("0") or ("0" if number else "")
    unit = re.sub(r"[\d" + _CJK_NUM + r"]+", "", token)
    return "{}#{}".format(unit or "N", number)


def ordinal_tokens(value: Optional[str]) -> Set[str]:
    """抽取序数/期次/年份特征。``红沙泉二矿项目`` → {'矿#2'}。"""
    text = unicodedata.normalize("NFKC", value or "")
    return {_canonical_ordinal(m.group(0)) for m in _ORDINAL.finditer(text)}


def ordinal_conflict(left: Optional[str], right: Optional[str]) -> bool:
    """**两边都带序数且不同** → 硬否决，不问模型。

    ``红沙泉一矿项目`` vs ``红沙泉二矿项目`` → True（不许自动归一）
    ``2025 年网络安全防护项目`` vs ``2026 年网络安全防护项目`` → True
    ``集团总部2025 年网络安全防护项目`` vs ``2025 年网络安全防护项目`` → False

    只有一边带序数时**不否决**：``红沙泉项目`` vs ``红沙泉二矿项目`` 正是需求
    第十节点名要归一的情形（把 ``红沙泉项目`` 规范成 ``红沙泉二矿项目``）。
    这类交给 LLM 判断，并由 :func:`ambiguous_ordinal` 挡住"简称同时匹配
    一矿和二矿"的危险情形。
    """
    a, b = ordinal_tokens(left), ordinal_tokens(right)
    return bool(a and b and a != b)


def ambiguous_ordinal(bare: Optional[str], candidates: List[str]) -> bool:
    """无序数的简称同时匹配到多个序数不同的候选 → 必须 UNCERTAIN。

    ``红沙泉项目`` 在同时出现 ``红沙泉一矿项目`` 和 ``红沙泉二矿项目`` 的会议里
    无法确定归属，这时候归一到任何一个都是赌博。
    """
    if ordinal_tokens(bare):
        return False
    seen = {frozenset(ordinal_tokens(name)) for name in candidates}
    seen.discard(frozenset())
    return len(seen) > 1


def char_ngrams(value: str, size: int = 2) -> Set[str]:
    text = normalize_name(value)
    if len(text) < size:
        return {text} if text else set()
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def jaccard(left: str, right: str, size: int = 2) -> float:
    a, b = char_ngrams(left, size), char_ngrams(right, size)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(left: str, right: str) -> bool:
    """一个名字是否为另一个的子串（归一化后）。别名最常见的形态。"""
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b or a == b:
        return False
    return a in b or b in a


def overlap_coefficient(left: str, right: str, size: int = 2) -> float:
    """重叠系数：短文本被长文本包含时接近 1，比 Jaccard 更适合长短标题比较。"""
    a, b = char_ngrams(left, size), char_ngrams(right, size)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen, result = set(), []
    for value in values:
        if value is None:
            continue
        key = value.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def grounding_ratio(candidate: str, sources: Iterable[str], size: int = 2) -> float:
    """candidate 的 n-gram 有多少比例能在 sources 里找到。

    用于校验 LLM 生成的 title 没有凭空造事实。纯标点/空白不计入。
    """
    grams = char_ngrams(candidate, size)
    if not grams:
        return 1.0
    pool: Set[str] = set()
    for source in sources:
        pool |= char_ngrams(source or "", size)
    if not pool:
        return 0.0
    return len(grams & pool) / len(grams)
