# -*- coding: utf-8 -*-
"""M1 末端基础质量门。

只做 M1 该做的基础检查，**不做**下游的历史任务关联与生命周期判断：
不判断两个 Item 是否其实是一件事，不做项目别名归一，不判断 CREATE/UPDATE。

检查不通过时不会静默把结果改成"看起来正常"的样子，
而是产出结构化问题列表；调用方据此决定 retry / fallback / 直接失败。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from schema_validator import validate_items
from text_normalizer import NormalizedDocument

CONTENT_OVERRUN_RATIO = 1.6
CONTENT_OVERRUN_SLACK = 20
# 跨项目串内容检查的对照集只用"本文档中真实存在的兄弟项目名"。
# 早期版本用 r"[一-龥A-Za-z0-9]{2,20}?(?:项目|平台|系统|专项|工程)" 正则去抓，
# 会把"墙面工程""保障项目""解决管控平台"全当成项目名，31 条告警几乎全是误报。
MIN_PROJECT_NAME_CHARS = 4

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass
class Issue:
    index: int
    code: str
    severity: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _sibling_projects(items: List[dict]) -> List[str]:
    """本次抽取中出现过的项目名，作为跨项目串内容的对照集。"""
    names = {
        (item.get("project") or "").strip()
        for item in items
        if isinstance(item, dict) and item.get("project")
    }
    return sorted(
        (n for n in names if len(n) >= MIN_PROJECT_NAME_CHARS), key=len, reverse=True
    )


def check_items(
    payload: dict, doc: Optional[NormalizedDocument] = None
) -> List[Issue]:
    issues: List[Issue] = []

    # 1) Schema
    for message in validate_items(payload, strict=False):
        issues.append(Issue(-1, "SCHEMA_INVALID", SEVERITY_ERROR, message))
    items = payload.get("items")
    if not isinstance(items, list):
        return issues

    doc_text = _compact(doc.text) if doc is not None else ""
    known_projects = _sibling_projects(items)

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence") or {}
        evidence_text = evidence.get("text") or ""
        content = item.get("content") or ""
        title = item.get("title") or ""

        # 6) 空 title / content
        if not title.strip():
            issues.append(Issue(index, "EMPTY_TITLE", SEVERITY_ERROR, "title 为空"))
        if not content.strip():
            issues.append(Issue(index, "EMPTY_CONTENT", SEVERITY_ERROR, "content 为空"))

        # 7) 非法 item_type 已由 Schema 覆盖，这里只补充空值场景
        if not item.get("item_type"):
            issues.append(Issue(index, "MISSING_ITEM_TYPE", SEVERITY_ERROR, "item_type 缺失"))

        # 2) evidence.text 能否在原文中找到
        if doc is not None:
            if not evidence_text.strip():
                issues.append(
                    Issue(index, "EMPTY_EVIDENCE", SEVERITY_ERROR, "evidence.text 为空")
                )
            elif not evidence.get("exact_match"):
                issues.append(
                    Issue(
                        index,
                        "EVIDENCE_NOT_LOCATED",
                        SEVERITY_WARNING,
                        "evidence.text 未能在原文中完整定位，已如实标记 exact_match=false",
                    )
                )

        # 3) assignee 是否有原文依据
        for name in item.get("assignee") or []:
            if doc is not None and name and name not in doc_text:
                issues.append(
                    Issue(
                        index,
                        "ASSIGNEE_NOT_IN_SOURCE",
                        SEVERITY_ERROR,
                        "assignee {!r} 在原文中不存在".format(name),
                    )
                )

        # 4) project 是否有原文依据
        project = item.get("project")
        if doc is not None and project and _compact(project) not in doc_text:
            issues.append(
                Issue(
                    index,
                    "PROJECT_NOT_IN_SOURCE",
                    SEVERITY_ERROR,
                    "project {!r} 在原文中不存在（M1 禁止自行归一项目名）".format(project),
                )
            )

        # 5) content 是否严重超出 evidence
        compact_content = _compact(content)
        compact_evidence = _compact(evidence_text)
        if compact_evidence and len(compact_content) > (
            CONTENT_OVERRUN_RATIO * len(compact_evidence) + CONTENT_OVERRUN_SLACK
        ):
            issues.append(
                Issue(
                    index,
                    "CONTENT_EXCEEDS_EVIDENCE",
                    SEVERITY_WARNING,
                    "content 长度 {} 远超 evidence {}".format(
                        len(compact_content), len(compact_evidence)
                    ),
                )
            )

        # 8) 明显跨项目串内容：content 里出现了**另一个真实兄弟项目**的名字。
        #    "红沙泉项目" ⊂ "红沙泉二矿项目" 这类包含关系不算串台。
        if project and known_projects:
            others = [
                name
                for name in known_projects
                if name != project
                and name in content
                and name not in project
                and project not in name
            ]
            if others:
                issues.append(
                    Issue(
                        index,
                        "CROSS_PROJECT_CONTENT",
                        SEVERITY_WARNING,
                        "content 中混入其他项目名 {}".format(others[:3]),
                    )
                )

    return issues


def summarize(issues: List[Issue]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for issue in issues:
        summary[issue.code] = summary.get(issue.code, 0) + 1
    summary["_errors"] = sum(1 for i in issues if i.severity == SEVERITY_ERROR)
    summary["_warnings"] = sum(1 for i in issues if i.severity == SEVERITY_WARNING)
    return summary
