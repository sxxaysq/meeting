"""Lexical candidate recall; scores never decide lifecycle actions."""

from __future__ import annotations

import re
from typing import Any

from .models import SourceContext, TaskStatus
from .repository import TaskRepository


NORMALIZE_PATTERN = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
ACTIVE = {
    TaskStatus.OPEN.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.BLOCKED.value,
}
CLOSED = {
    TaskStatus.COMPLETED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.CLOSED.value,
}


def normalize_text(value: Any) -> str:
    return NORMALIZE_PATTERN.sub("", str(value or "")).casefold()


def bigrams(value: Any) -> set[str]:
    normalized = normalize_text(value)
    if not normalized:
        return set()
    if len(normalized) == 1:
        return {normalized}
    return {
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
    }


def dice(left: Any, right: Any) -> float:
    left_terms = bigrams(left)
    right_terms = bigrams(right)
    if not left_terms or not right_terms:
        return 0.0
    return 2 * len(left_terms & right_terms) / (
        len(left_terms) + len(right_terms)
    )


def project_compatible(source, task):
    """An uncertain project stays independent; no fallback to unrelated projects."""
    left, right = source.project_entity_id, task.get('project_entity_id')
    a, b = normalize_text(source.item.get('project')), normalize_text(task.get('project'))
    # Explicit mine/phase/year differences remain incompatible even with a bad shared ID.
    qualifiers = lambda text: set(re.findall(r'(?:[一二三四五六七八九十\d]+矿|[一二三四五六七八九十\d]+期|20\d{2})', text))
    qa, qb = qualifiers(a), qualifiers(b)
    if qa and qb and qa != qb:
        return False
    # Explicit hierarchy is a scope relation, never a permanent alias merge.
    for parent,child in ((source.item.get('project'),task.get('project')),
                         (task.get('project'),source.item.get('project'))):
        match=re.fullmatch(r'(.+项目)\s*[-—–:：]\s*(.+)',str(child or ''))
        if match and normalize_text(match[1])==normalize_text(parent):
            sub=normalize_text(match[2])
            current_text=normalize_text(source.item['title']+' '+source.item['content'])
            target_text=normalize_text(task.get('title','')+' '+task.get('description',task.get('content','')))
            if len(sub)>=2 and sub in current_text and sub in target_text:
                return True
    if left and right:
        return left == right
    return a == b


def same_goal(left, right):
    content=normalize_text(left.get('content'))
    other=normalize_text(right.get('content',right.get('description')))
    title=normalize_text(left.get('title'))
    goal=title.replace(normalize_text(left.get('project')),'') if left.get('project') else title
    return (len(content)>=8 and content==other) or (
        len(goal)>=4 and goal not in {'持续推进','项目推进','现场跟进','日常工作'}
        and title==normalize_text(right.get('title')))


def _assignee_overlap(current: list[str], historical: list[str]) -> float:
    left = {normalize_text(value) for value in current if normalize_text(value)}
    right = {
        normalize_text(value) for value in historical if normalize_text(value)
    }
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class CandidateRetriever:
    def __init__(
        self,
        repository: TaskRepository,
        *,
        top_k: int = 5,
        closed_quota: int = 2,
        recent_event_limit: int = 5,
        semantic_search=None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        self.repository = repository
        self.top_k = top_k
        self.closed_quota = min(max(0, closed_quota), top_k)
        self.recent_event_limit = recent_event_limit
        self.semantic_search = semantic_search
        self.index_diagnostics = []

    def retrieve(self, source: SourceContext, *, expanded=False) -> list[dict[str, Any]]:
        tasks = self.repository.retrieval_tasks(self.recent_event_limit)
        if not tasks:
            return []
        pool = [task for task in tasks if project_compatible(source,task)]
        semantic = {}
        # Optional existing semantic index. Hits never supply authoritative task fields.
        # Missing/stale/future/foreign IDs fall back to this database's lexical retrieval.
        if self.semantic_search:
            try:
                current = {task['task_id']:task for task in pool}
                for hit in self.semantic_search(source.item['title']+'\n'+source.item['content'], 20):
                    task = current.get(hit.get('task_id'))
                    if (task and hit.get('version') == task['version']
                            and hit.get('last_event_id') == task.get('last_event_id')):
                        score=float(hit['score'])
                        if 0 <= score <= 1: semantic[task['task_id']]=score
                    else:
                        self.index_diagnostics.append('stale_or_foreign_hit')
            except Exception as error:
                self.index_diagnostics.append(type(error).__name__)
        limit = max(self.top_k,12) if expanded else self.top_k
        quota = min(4,limit) if expanded else self.closed_quota
        ranked = sorted(
            ((score + 0.2*semantic.get(task['task_id'],0), reasons, task)
             for score,reasons,task in (self._score(source, task) for task in pool)),
            key=lambda entry: (-entry[0], entry[2]["task_id"]),
        )
        active = [entry for entry in ranked if entry[2]["status"] in ACTIVE]
        closed = [entry for entry in ranked if entry[2]["status"] in CLOSED]
        active_limit = limit - quota
        if active:
            active_limit = max(1, active_limit)
        selected = active[:active_limit]
        selected.extend(closed[: quota])
        if len(selected) < limit:
            chosen_ids = {entry[2]["task_id"] for entry in selected}
            selected.extend(
                entry
                for entry in ranked
                if entry[2]["task_id"] not in chosen_ids
            )
        selected = sorted(
            selected[: limit],
            key=lambda entry: (-entry[0], entry[2]["task_id"]),
        )
        return [self._candidate_view(*entry) for entry in selected]

    @staticmethod
    def _project_scope(
        source: SourceContext,
        tasks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if source.item["item_type"] != "PROJECT_TASK":
            return []
        if source.project_entity_id:
            matched = [
                task
                for task in tasks
                if task.get("project_entity_id") == source.project_entity_id
            ]
            if matched:
                return matched
        project = normalize_text(source.item.get("project"))
        if project:
            matched = [
                task
                for task in tasks
                if normalize_text(task.get("project")) == project
            ]
            if matched:
                return matched
        return []

    @staticmethod
    def _score(
        source: SourceContext,
        task: dict[str, Any],
    ) -> tuple[float, list[str], dict[str, Any]]:
        item = source.item
        event_text = "\n".join(
            event["content"] for event in task.get("recent_events", [])
        )
        def goal_title(row):
            title=normalize_text(row.get('title'))
            project=normalize_text(row.get('project'))
            return title.replace(project,'') if project else title
        title_score = dice(goal_title(item), goal_title(task))
        content_score = dice(item.get("content"), task.get("description"))
        event_score = dice(item.get("content"), event_text)
        assignee_score = _assignee_overlap(
            item.get("assignee") or [], task.get("assignees") or []
        )
        score = (
            0.40 * title_score
            + 0.30 * content_score
            + 0.20 * event_score
            + 0.10 * assignee_score
        )
        reasons = [
            f"title:{title_score:.3f}",
            f"description:{content_score:.3f}",
            f"events:{event_score:.3f}",
            f"assignee:{assignee_score:.3f}",
        ]
        if source.project_entity_id and (
            task.get("project_entity_id") == source.project_entity_id
        ):
            reasons.append("same_project_entity")
        if (
            normalize_text(item.get("department"))
            and normalize_text(item.get("department"))
            == normalize_text(task.get("department"))
        ):
            score += 0.05
            reasons.append("same_department")
        if task["status"] in ACTIVE:
            score += 0.03
            reasons.append("active_priority")
        return min(score, 1.0), reasons, task

    @staticmethod
    def _candidate_view(
        score: float,
        reasons: list[str],
        task: dict[str, Any],
    ) -> dict[str, Any]:
        fields = (
            "task_id",
            "item_type",
            "project_entity_id",
            "project",
            "department_id",
            "department",
            "work_section",
            "delivery_group",
            "title",
            "description",
            "assignees",
            "status",
            "version",
            "created_at",
            "updated_at",
            "recent_events",
        )
        candidate = {field: task.get(field) for field in fields}
        candidate["retrieval_score"] = round(score, 6)
        candidate["retrieval_reasons"] = reasons
        return candidate
