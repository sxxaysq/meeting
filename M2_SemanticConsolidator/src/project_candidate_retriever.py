# -*- coding: utf-8 -*-
"""项目实体归一的候选召回。

只做召回，不做判定。召回信号：
1. 归一化后完全相同 → 直接同实体，不用问模型；
2. 包含关系（``互联网收敛项目`` ⊂ ``集团互联网收敛项目``）；
3. 核心名（剥掉"项目/平台/系统"尾缀）相同或高度重合；
4. 字符 2-gram Jaccard 超阈值。

**序数冲突一票否决**：``红沙泉一矿`` vs ``红沙泉二矿``、``2025 年`` vs ``2026 年``
字面极近但业务上必然不同，这类根本不进候选，避免把判断压力丢给模型。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from text_utils import (
    containment,
    core_name,
    jaccard,
    normalize_name,
    ordinal_conflict,
)

# 召回阈值刻意放宽——漏召回就永远没机会归一，误召回还有 LLM 兜底。
JACCARD_RECALL = 0.55
CORE_JACCARD_RECALL = 0.70


def collect_project_names(items: Iterable[dict]) -> List[str]:
    """按首次出现顺序收集本次会议里出现过的项目原始写法。"""
    seen, names = set(), []
    for item in items:
        value = item.get("project")
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value:
            continue
        key = normalize_name(value)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(value)
    return names


# 核心名互相包含时，短的一方至少要这么长，否则太容易误召回
MIN_CORE_CHARS = 2


def _core_containment(left: str, right: str) -> bool:
    if not left or not right or left == right:
        return False
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return len(shorter) >= MIN_CORE_CHARS and shorter in longer


def recall_signals(left: str, right: str) -> Optional[Dict[str, object]]:
    """返回召回理由；不该进候选时返回 None。"""
    if normalize_name(left) == normalize_name(right):
        return {"rule": "exact", "jaccard": 1.0}
    if ordinal_conflict(left, right):
        return None
    signals: Dict[str, object] = {}
    if containment(left, right):
        signals["rule"] = "containment"
    core_left, core_right = core_name(left), core_name(right)
    core_score = jaccard(core_left, core_right)
    full_score = jaccard(left, right)
    if core_left and core_left == core_right:
        signals.setdefault("rule", "same_core")
    elif _core_containment(core_left, core_right):
        # 简称形态：``红沙泉项目`` 的核心名 ``红沙泉`` 是 ``红沙泉二矿`` 的前缀。
        # 整名互不包含（"红沙泉二矿项目" 里没有 "红沙泉项目" 这个连续串），
        # 光靠 containment 和 n-gram 都召不回来。
        signals.setdefault("rule", "core_containment")
    elif core_score >= CORE_JACCARD_RECALL:
        signals.setdefault("rule", "core_similar")
    elif full_score >= JACCARD_RECALL:
        signals.setdefault("rule", "ngram_similar")
    if "rule" not in signals:
        return None
    signals["jaccard"] = round(full_score, 3)
    signals["core_jaccard"] = round(core_score, 3)
    return signals


def pair_candidates(names: List[str]) -> List[Tuple[str, str, Dict[str, object]]]:
    """本次会议内部的项目名两两候选。"""
    candidates = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            signals = recall_signals(names[i], names[j])
            if signals:
                candidates.append((names[i], names[j], signals))
    return candidates


def catalog_candidates(
    name: str, catalog_entries: List[Dict[str, object]], limit: int = 5
) -> List[Dict[str, object]]:
    """从已有项目主表里召回可能的同实体项目，按相似度排序。"""
    scored = []
    for entry in catalog_entries:
        best = None
        for alias in [entry["canonical_name"], *entry.get("aliases", [])]:
            signals = recall_signals(name, str(alias))
            if not signals:
                continue
            score = max(float(signals.get("jaccard", 0)), float(signals.get("core_jaccard", 0)))
            if best is None or score > best[0]:
                best = (score, signals)
        if best:
            scored.append((best[0], entry, best[1]))
    scored.sort(key=lambda row: row[0], reverse=True)
    return [
        {"entity": entry, "signals": signals} for _score, entry, signals in scored[:limit]
    ]
