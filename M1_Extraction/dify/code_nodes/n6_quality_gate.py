# -*- coding: utf-8 -*-
"""Dify Code 节点 6：基础 Quality Gate。

只做 M1 该做的基础检查，不做下游的历史任务关联与生命周期判断。
检查不通过时不静默修正，而是产出 issues 与 has_error 供后续分支判断。

输入：items_json、doc_text
输出：issues_json、error_count、warning_count、has_error
"""

import json
import re

ITEM_TYPES = ("PROJECT_TASK", "RESEARCH_TASK", "NON_PROJECT_WORK", "NON_TASK_ITEM")
CONTENT_OVERRUN_RATIO = 1.6
CONTENT_OVERRUN_SLACK = 20
# 跨项目串内容的对照集只用"本次抽取中真实出现过的兄弟项目名"。
# 用正则去抓 xx项目/xx平台/xx系统 会把"墙面工程""保障项目"也算进来，几乎全是误报。
MIN_PROJECT_NAME_CHARS = 4


def compact(text):
    return re.sub(r"\s+", "", text or "")


def main(items_json: str, doc_text: str) -> dict:
    payload = json.loads(items_json or '{"items":[]}')
    items = payload.get("items") or []
    doc_compact = compact(doc_text)
    known_projects = sorted(
        {
            (i.get("project") or "").strip()
            for i in items
            if (i.get("project") or "").strip()
            and len((i.get("project") or "").strip()) >= MIN_PROJECT_NAME_CHARS
        },
        key=len,
        reverse=True,
    )

    issues = []

    def add(index, code, severity, detail):
        issues.append(
            {"index": index, "code": code, "severity": severity, "detail": detail}
        )

    for index, item in enumerate(items):
        evidence = item.get("evidence") or {}
        evidence_text = evidence.get("text") or ""
        content = item.get("content") or ""

        if not (item.get("title") or "").strip():
            add(index, "EMPTY_TITLE", "error", "title 为空")
        if not content.strip():
            add(index, "EMPTY_CONTENT", "error", "content 为空")
        if item.get("item_type") not in ITEM_TYPES:
            add(index, "ILLEGAL_ITEM_TYPE", "error", "非法 item_type: %r" % (item.get("item_type"),))
        if not isinstance(item.get("assignee"), list):
            add(index, "ASSIGNEE_NOT_ARRAY", "error", "assignee 必须是数组")

        if not evidence_text.strip():
            add(index, "EMPTY_EVIDENCE", "error", "evidence.text 为空")
        elif not evidence.get("exact_match"):
            add(
                index,
                "EVIDENCE_NOT_LOCATED",
                "warning",
                "evidence.text 未能完整定位，已如实标记 exact_match=false",
            )

        for name in item.get("assignee") or []:
            if name and name not in doc_compact:
                add(index, "ASSIGNEE_NOT_IN_SOURCE", "error", "assignee %r 原文中不存在" % (name,))

        project = item.get("project")
        if project and compact(project) not in doc_compact:
            add(
                index,
                "PROJECT_NOT_IN_SOURCE",
                "error",
                "project %r 原文中不存在（M1 禁止自行归一项目名）" % (project,),
            )

        compact_content = compact(content)
        compact_evidence = compact(evidence_text)
        if compact_evidence and len(compact_content) > (
            CONTENT_OVERRUN_RATIO * len(compact_evidence) + CONTENT_OVERRUN_SLACK
        ):
            add(
                index,
                "CONTENT_EXCEEDS_EVIDENCE",
                "warning",
                "content 长度 %d 远超 evidence %d" % (len(compact_content), len(compact_evidence)),
            )

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
                add(
                    index,
                    "CROSS_PROJECT_CONTENT",
                    "warning",
                    "content 中混入其他项目名 %s" % (others[:3],),
                )

    error_count = sum(1 for i in issues if i["severity"] == "error")
    warning_count = len(issues) - error_count
    return {
        "issues_json": json.dumps(issues, ensure_ascii=False),
        "error_count": error_count,
        "warning_count": warning_count,
        "has_error": "yes" if error_count else "no",
    }
