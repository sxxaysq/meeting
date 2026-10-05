"""Hermetic tests for the semantic layer: resolver, reader, retrieval, index.

Everything here runs against in-process doubles — no Neo4j, no GPU service, no
model. The live end-to-end behaviour is exercised by ``probe_resolver.py`` and
the full replay; these tests pin the decision rules so a refactor cannot quietly
change them.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from M3_KnowledgeGraph.src.project_resolver import ProjectResolver  # noqa: E402
from M3_KnowledgeGraph.src.semantic_reader import SemanticReader  # noqa: E402
from M3_KnowledgeGraph.src.semantic_memory import (  # noqa: E402
    normalize_surface,
    stable_key,
)
from M3_KnowledgeGraph.src import task_retrieval  # noqa: E402


# --------------------------------------------------------------------- doubles


class FakeEmbedder:
    """Deterministic char-bigram vectors; offline and reproducible."""

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions
        self.calls = 0
        self.texts_embedded = 0
        self.cache_hits = 0
        self.rerank_calls = 0

    @staticmethod
    def _terms(text: str) -> list[str]:
        value = "".join(ch for ch in str(text or "") if not ch.isspace()).casefold()
        if len(value) < 2:
            return [value] if value else []
        return [value[i : i + 2] for i in range(len(value) - 1)]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for term in self._terms(text):
            digest = hashlib.sha256(term.encode("utf-8")).digest()
            vector[digest[0] % self.dimensions] += 1.0 if digest[1] % 2 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    def embed_one(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts_embedded += len(texts)
        return [self._vector(text) for text in texts]

    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        self.rerank_calls += 1
        return []

    def stats(self) -> dict[str, int]:
        return {"embedding_calls": self.calls, "texts_embedded": self.texts_embedded,
                "cache_hits": self.cache_hits, "cache_size": 0,
                "rerank_calls": self.rerank_calls}


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    nl = math.sqrt(sum(a * a for a in left))
    nr = math.sqrt(sum(b * b for b in right))
    return dot / (nl * nr) if nl and nr else 0.0


class FakeMemory:
    """Implements the slice of ``SemanticMemory`` the resolver and reader use."""

    def __init__(self, embedder: FakeEmbedder) -> None:
        self.embedder = embedder
        self.surfaces: dict[str, dict[str, Any]] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self.rules: dict[str, dict[str, Any]] = {}
        self.semantics: dict[str, dict[str, Any]] = {}
        self.task_links: list[tuple[str, str]] = []

    # write
    def observe_surface(self, text, *, document, authority="FIELD", embedding=None, task_id=None):
        key = stable_key("surface", normalize_surface(text))
        row = self.surfaces.setdefault(key, {
            "text": text, "status": "UNRESOLVED", "frequency": 0, "field_count": 0,
            "content_count": 0, "first_document": document, "canonical_name": None,
            "family_key": None, "project_key": None, "reason": None, "score": None,
            "decided_by": None, "embedding": embedding or self.embedder.embed_one(text),
        })
        row["frequency"] += 1
        row["last_document"] = document
        if authority == "FIELD":
            row["field_count"] += 1
        else:
            row["content_count"] += 1

    def upsert_project(self, canonical_name, *, family_key, family_text, document):
        project_key = stable_key("project", normalize_surface(canonical_name))
        self.projects.setdefault(project_key, {
            "project_key": project_key, "canonical_name": canonical_name,
            "family_key": family_key, "family_text": family_text,
            "first_document": document, "task_count": 0,
        })
        return project_key

    def bind_surface(self, text, *, canonical_name, family_key, family_text, status,
                     decided_by, reason, document, score=None, corrected_from=None,
                     embedding=None):
        # Mirrors the real MERGE: binding records the decision, it does not count
        # another observation. Only observe_surface bumps frequency/field_count.
        key = stable_key("surface", normalize_surface(text))
        if key not in self.surfaces:
            self.surfaces[key] = {
                "text": text, "status": "UNRESOLVED", "frequency": 0, "field_count": 0,
                "content_count": 0, "first_document": document, "canonical_name": None,
                "family_key": None, "project_key": None, "reason": None, "score": None,
                "decided_by": None,
                "embedding": embedding if embedding is not None else self.embedder.embed_one(text),
            }
        row = self.surfaces[key]
        project_key = self.upsert_project(canonical_name, family_key=family_key,
                                          family_text=family_text, document=document)
        row.update(status=status, decided_by=decided_by, reason=reason, score=score,
                   canonical_name=canonical_name, family_key=family_key,
                   project_key=project_key, last_document=document)
        if corrected_from:
            row["corrected_to"] = corrected_from
        return {"project_key": project_key, "surface_key": key}

    def remember_rule(self, *, kind, premise, conclusion, decided_by, reason, document,
                      evidence="", family_key=None):
        rule_key = stable_key("rule", kind, normalize_surface(premise), normalize_surface(conclusion))
        self.rules[rule_key] = {"rule_key": rule_key, "kind": kind, "premise": premise,
                                "conclusion": conclusion, "decided_by": decided_by,
                                "reason": reason, "family_key": family_key, "hits": 0,
                                "evidence": evidence, "first_document": document}
        return rule_key

    def count_rule_hit(self, rule_key):
        if rule_key in self.rules:
            self.rules[rule_key]["hits"] += 1

    def link_task(self, project_key, task_id, *, document):
        self.task_links.append((project_key, task_id))

    def remember_semantic(self, kind, evidence, flags, *, subject="", reason=""):
        key = stable_key("semantic", kind, normalize_surface(evidence), normalize_surface(subject))
        self.semantics[key] = {"flags": dict(flags), "reason": reason, "hits": 0}

    def bump_semantic_hit(self, kind, evidence, subject=""):
        key = stable_key("semantic", kind, normalize_surface(evidence), normalize_surface(subject))
        if key in self.semantics:
            self.semantics[key]["hits"] += 1

    # read
    def lookup_surface(self, text):
        return dict(self.surfaces.get(stable_key("surface", normalize_surface(text))) or {}) or None

    def vector_recall(self, text, *, k=24):
        query = self.embedder.embed_one(text)
        rows = []
        for row in self.surfaces.values():
            score = _cosine(query, row["embedding"])
            rows.append({**row, "score": score, "project_name": row.get("canonical_name"),
                         "task_count": 0})
        rows.sort(key=lambda r: -r["score"])
        return rows[:k]

    def family_rule(self, family_key):
        for rule in self.rules.values():
            if rule["kind"] == "FAMILY_COLLAPSE" and rule["family_key"] == family_key:
                return dict(rule)
        return None

    def find_rule(self, kind, premise, conclusion):
        key = stable_key("rule", kind, normalize_surface(premise), normalize_surface(conclusion))
        return dict(self.rules[key]) if key in self.rules else None

    def family_members(self, family_key):
        return [dict(r) for r in self.surfaces.values() if r.get("family_key") == family_key]

    def semantic_flags(self, kind, evidence, subject=""):
        key = stable_key("semantic", kind, normalize_surface(evidence), normalize_surface(subject))
        row = self.semantics.get(key)
        return dict(row["flags"]) if row else None

    def export_snapshot(self):
        return {"projects": list(self.projects.values()),
                "surfaces": list(self.surfaces.values()),
                "rules": list(self.rules.values()),
                "semantics": list(self.semantics.values())}

    def stats(self):
        return {"projects": len(self.projects), "surfaces": len(self.surfaces),
                "rules": len(self.rules), "semantic_cache": len(self.semantics)}


class ScriptedClient:
    """Returns queued model answers; records what it was asked."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.payloads: list[dict[str, Any]] = []
        self.model = "scripted"

    def decide(self, prompt, payload, correction=None):
        self.prompts.append(prompt)
        self.payloads.append(payload)
        if not self.answers:
            raise AssertionError("model called more often than scripted")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, {"prompt_chars": len(prompt)}


def make_resolver(answers, *, embedder=None, threshold=0.30):
    embedder = embedder or FakeEmbedder()
    memory = FakeMemory(embedder)
    client = ScriptedClient(answers)
    resolver = ProjectResolver(memory, client, family_threshold=threshold,
                               candidates_per_name=6)
    return resolver, memory, client, embedder


def decision(idx=0, family="红沙泉", canonical_index=None, status="CANONICAL", reason="r"):
    return {"results": [{"idx": idx, "family": family, "canonical_index": canonical_index,
                         "status": status, "reason": reason}]}


# ------------------------------------------------------- family collapse rules


def test_first_surface_becomes_canonical_and_records_a_rule():
    resolver, memory, client, _ = make_resolver([decision(status="CANONICAL")])
    result = resolver.resolve("红沙泉项目", document="D1", authority="FIELD",
                              context={"title": "红沙泉项目", "content": "现场跟进。"})
    assert result.status == "CANONICAL" and result.decided_by == "LLM"
    assert result.canonical_name == "红沙泉项目" and result.family_text == "红沙泉"
    assert result.project_key == stable_key("project", normalize_surface("红沙泉项目"))
    rules = [r for r in memory.rules.values() if r["kind"] == "FAMILY_COLLAPSE"]
    assert len(rules) == 1 and rules[0]["premise"] == "红沙泉"
    assert rules[0]["conclusion"] == "红沙泉项目"


def test_sibling_mine_number_collapses_into_the_family():
    """红沙泉二矿项目 → 红沙泉项目: the mine number is no longer a distinction."""
    resolver, memory, client, _ = make_resolver([
        decision(status="CANONICAL"),
        decision(idx=0, canonical_index=0, status="ALIAS", reason="同矿区专名主体"),
    ])
    resolver.resolve("红沙泉项目", document="D1", context={"title": "红沙泉项目", "content": "跟进。"})
    second = resolver.resolve("红沙泉二矿项目", document="D1",
                              context={"title": "红沙泉二矿集控中心装修", "content": "装修及空调到货。"})
    assert second.status == "ALIAS" and second.canonical_name == "红沙泉项目"
    assert second.project_key == stable_key("project", normalize_surface("红沙泉项目"))
    # One canonical project node, two surfaces pointing at it.
    assert len(memory.projects) == 1 and len(memory.surfaces) == 2
    offered = [c["text"] for c in second.candidates]
    assert "红沙泉项目" in offered, "召回必须把已知规范名交给模型"


def test_second_resolution_is_free_and_comes_from_memory():
    resolver, memory, client, _ = make_resolver([decision(status="CANONICAL")])
    first = resolver.resolve("红沙泉项目", document="D1", context={"title": "红沙泉项目", "content": "跟进。"})
    calls_after_first = client.prompts.__len__()
    again = resolver.resolve("红沙泉项目", document="D2", context={"title": "红沙泉项目", "content": "继续。"})
    assert again.decided_by == "MEMORY" and again.canonical_name == first.canonical_name
    assert len(client.prompts) == calls_after_first, "已记住的称谓不得再问模型"
    assert resolver.stats()["memory_hits"] == 1


def test_batch_resolution_asks_once_per_batch():
    answers = [{"results": [
        {"idx": 0, "family": "红沙泉", "canonical_index": None, "status": "CANONICAL", "reason": "a"},
        {"idx": 1, "family": "陶忽图", "canonical_index": None, "status": "CANONICAL", "reason": "b"},
    ]}]
    resolver, memory, client, _ = make_resolver(answers)
    out = resolver.resolve_batch(
        [("红沙泉项目", "FIELD", {"title": "红沙泉项目", "content": "跟进。"}),
         ("陶忽图项目", "FIELD", {"title": "陶忽图项目", "content": "投标。"})],
        document="D1")
    assert len(client.prompts) == 1, "整批只应调用一次模型"
    assert out[normalize_surface("红沙泉项目")].family_text == "红沙泉"
    assert out[normalize_surface("陶忽图项目")].family_text == "陶忽图"
    assert len(memory.projects) == 2


# ------------------------------------------------------------ typo correction


def test_typo_is_corrected_toward_the_better_attested_spelling():
    """乌冬项目 → 乌东项目, decided by authority, not by similarity."""
    resolver, memory, client, _ = make_resolver([
        decision(family="乌东", status="CANONICAL"),
        decision(idx=0, family="乌东", canonical_index=0, status="TYPO",
                 reason="冬与东音同形近，候选出现在权威项目字段"),
    ])
    resolver.resolve("乌东项目", document="D1", authority="FIELD",
                     context={"title": "乌东项目测试及调试", "content": "测试泵站数据。"})
    resolver.resolve("乌东项目", document="D2", authority="FIELD",
                     context={"title": "跟进乌东项目运行检测", "content": "跟进泵站运行检测。"})
    typo = resolver.resolve("乌冬项目", document="D3", authority="CONTENT",
                            context={"title": "计划采购评审6项",
                                     "content": "公开招标乌冬项目无人化综放工作面采购"})
    assert typo.status == "TYPO" and typo.canonical_name == "乌东项目"
    rules = [r for r in memory.rules.values() if r["kind"] == "TYPO_MAP"]
    assert len(rules) == 1
    assert rules[0]["premise"] == "乌冬项目" and rules[0]["conclusion"] == "乌东项目"
    surface = memory.lookup_surface("乌冬项目")
    assert surface["corrected_to"] == "乌东项目"
    # The rule is reusable: the same typo later costs no model call.
    calls = len(client.prompts)
    replay = resolver.resolve("乌冬项目", document="D4", authority="CONTENT",
                              context={"title": "采购", "content": "乌冬项目采购"})
    assert replay.decided_by == "MEMORY" and replay.canonical_name == "乌东项目"
    assert len(client.prompts) == calls


def test_typo_is_refused_when_the_candidate_has_no_authority():
    """A spelling never seen in the project field cannot become the correction."""
    resolver, memory, client, _ = make_resolver([
        decision(family="乌东", status="CANONICAL"),
        decision(idx=0, family="乌东", canonical_index=0, status="TYPO", reason="形近"),
    ])
    resolver.resolve("乌东项目", document="D1", authority="CONTENT",
                     context={"title": "采购", "content": "乌东项目采购"})
    result = resolver.resolve("乌冬项目", document="D2", authority="CONTENT",
                              context={"title": "乌冬项目", "content": "推进。"})
    assert result.status == "UNRESOLVED", "候选权威度不足时必须拒绝改写"
    assert "权威" in result.reason and "拒绝改写" in result.reason
    assert (memory.lookup_surface("乌冬项目") or {}).get("status") != "TYPO"


def test_typo_is_refused_when_the_rival_is_equally_attested():
    """Two one-off spellings give no basis to pick a winner."""
    resolver, memory, client, _ = make_resolver([
        decision(family="乌东", status="CANONICAL"),
        decision(idx=0, family="乌东", canonical_index=0, status="TYPO", reason="形近"),
    ])
    resolver.resolve("乌东项目", document="D1", authority="FIELD",
                     context={"title": "乌东项目", "content": "推进。"})
    result = resolver.resolve("乌冬项目", document="D2", authority="FIELD",
                              context={"title": "乌冬项目", "content": "推进。"})
    assert result.status == "UNRESOLVED"
    assert "不高于当前写法" in result.reason


# ------------------------------------------------------- refusing bad answers


def test_model_cannot_pick_outside_the_offered_candidates():
    resolver, memory, client, _ = make_resolver([
        decision(status="CANONICAL"),
        decision(idx=0, canonical_index=9, status="ALIAS", reason="越界"),
    ])
    resolver.resolve("红沙泉项目", document="D1", context={"title": "红沙泉项目", "content": "跟进。"})
    result = resolver.resolve("红沙泉二矿项目", document="D1",
                              context={"title": "红沙泉二矿项目", "content": "装修。"})
    assert result.status == "UNRESOLVED" and "不在提供的候选中" in result.reason


def test_model_cannot_invent_a_family_it_did_not_state():
    resolver, memory, client, _ = make_resolver([{"results": [
        {"idx": 0, "family": "", "canonical_index": None, "status": "CANONICAL", "reason": "无族"}]}])
    result = resolver.resolve("红沙泉项目", document="D1", context={"title": "红沙泉项目", "content": "跟进。"})
    assert result.status == "UNRESOLVED" and "family" in result.reason


def test_unknown_status_is_not_guessed_at():
    resolver, memory, client, _ = make_resolver([{"results": [
        {"idx": 0, "family": "红沙泉", "canonical_index": None, "status": "MAYBE", "reason": "?"}]}])
    result = resolver.resolve("红沙泉项目", document="D1", context={"title": "红沙泉项目", "content": "跟进。"})
    assert result.status == "UNRESOLVED" and "MAYBE" in result.reason


def test_cross_family_stays_independent_and_is_remembered():
    resolver, memory, client, _ = make_resolver([
        decision(status="CANONICAL"),
        {"results": [{"idx": 0, "family": "陶忽图", "canonical_index": None,
                      "status": "INDEPENDENT", "reason": "不同矿区专名"}]},
    ])
    resolver.resolve("红沙泉项目", document="D1", context={"title": "红沙泉项目", "content": "跟进。"})
    result = resolver.resolve("陶忽图项目", document="D1", context={"title": "陶忽图项目", "content": "投标。"})
    assert result.status == "INDEPENDENT" and result.canonical_name == "陶忽图项目"
    assert result.canonical_name != "红沙泉项目"
    assert any(r["kind"] == "KEEP_SEPARATE" for r in memory.rules.values())


def test_model_outage_leaves_the_name_unresolved_not_guessed():
    resolver, memory, client, _ = make_resolver([RuntimeError("service down")])
    result = resolver.resolve("红沙泉项目", document="D1", context={"title": "红沙泉项目", "content": "跟进。"})
    assert result.status == "UNRESOLVED"
    assert resolver.stats()["service_failures"] >= 1
    assert memory.projects == {}, "模型不可用时不得写入任何身份"


def test_missing_client_never_invents_an_identity():
    embedder = FakeEmbedder()
    memory = FakeMemory(embedder)
    resolver = ProjectResolver(memory, None)
    result = resolver.resolve("红沙泉项目", document="D1", context={"title": "红沙泉项目", "content": "跟进。"})
    assert result.status == "UNRESOLVED" and resolver.llm_calls == 0


# ------------------------------------------------------------- semantic reader


def reader_answers(*answers):
    memory = FakeMemory(FakeEmbedder())
    client = ScriptedClient(list(answers))
    return SemanticReader(memory, client), memory, client


def test_completion_reading_is_taken_from_the_model():
    reader, memory, client = reader_answers({
        "definite": True, "future_or_partial": False, "whole_goal": True,
        "phases_covered": False, "goal_in_evidence": True, "reason": "只完成装修"})
    flags = reader.read_completion(evidence="已完成数据中心装修。", context="已完成数据中心装修。",
                                   task_title="数据中心装修及安装", task_project="红沙泉项目")
    assert flags["phases_covered"] is False and flags["definite"] is True
    assert flags["cached"] is False and len(client.prompts) == 1


def test_completion_reading_is_remembered_and_reused():
    proven = {"definite": True, "future_or_partial": False, "whole_goal": True,
              "phases_covered": True, "goal_in_evidence": True, "reason": "全部完成"}
    reader, memory, client = reader_answers(proven, dict(proven))
    kwargs = dict(evidence="已完成数据中心建设。", context="已完成数据中心建设。",
                  task_title="建设数据中心", task_project="红沙泉项目")
    first = reader.read_completion(**kwargs)
    second = reader.read_completion(**kwargs)
    assert first["cached"] is False and second["cached"] is True
    assert len(client.prompts) == 1, "同一判读不得重复调用模型"
    assert reader.cache_hits == 1
    # A different task goal is a different question and is asked again.
    third = reader.read_completion(**{**kwargs, "task_title": "建设集控中心"})
    assert len(client.prompts) == 2 and third["cached"] is False
    # The cached answer carries no model reason, so it cannot be mistaken for one.
    assert second["reason"] == "记忆库复用"


def test_unreadable_completion_defaults_to_not_proven():
    """A malformed answer must never be read as 'the goal is complete'."""
    reader, memory, client = reader_answers({"definite": "yes"}, {"oops": True})
    flags = reader.read_completion(evidence="完成。", context="完成。", task_title="建设数据中心")
    assert flags["definite"] is False and flags["future_or_partial"] is True
    assert flags["whole_goal"] is False
    assert reader.unreadable == 1


def test_status_change_reading_reports_negation():
    reader, memory, client = reader_answers({"explicit": False, "negated": True, "reason": "原文说不要取消"})
    flags = reader.read_status_change("CANCEL", evidence="取消数据中心建设任务",
                                      context="不要取消数据中心建设任务", task_title="建设数据中心")
    assert flags["negated"] is True and flags["explicit"] is False
    with pytest.raises(ValueError):
        reader.read_status_change("COMPLETE", evidence="x", context="x")


def test_transfer_reading_uses_the_transfer_wording():
    reader, memory, client = reader_answers({"explicit": True, "negated": False, "reason": "明确改由研发中心"})
    flags = reader.read_status_change("TRANSFER", evidence="改由研发中心负责",
                                      context="该项目改由研发中心负责", task_title="建设数据中心")
    assert flags["explicit"] is True
    assert "移交" in client.prompts[0] or "TRANSFER" in json.dumps(client.payloads[0])


def test_rename_reading_defaults_to_no():
    reader, memory, client = reader_answers("not a dict")
    flags = reader.read_rename(evidence="推进现场施工", context="推进现场施工", task_title="建设数据中心")
    assert flags["explicit_rename"] is False and reader.unreadable == 1


# ------------------------------------------------------------- retrieval rules


@pytest.fixture(autouse=True)
def _isolate_semantics():
    task_retrieval.reset_semantics()
    yield
    task_retrieval.reset_semantics()


def test_same_goal_accepts_a_restatement_and_rejects_a_different_goal():
    task_retrieval.configure_semantics(FakeEmbedder())
    left = {"title": "跟进回款", "content": "跟进2024年度自主可控建设项目回款。", "project": "自主可控项目"}
    restated = {"title": "完成回款", "content": "完成2024年度自主可控建设项目30%回款。",
                "project": "自主可控项目"}
    other = {"title": "编制方案", "content": "编制梅岭铜金矿微震监测系统方案并与矿方交流。",
             "project": "梅岭项目"}
    assert task_retrieval.same_goal(left, left) is True, "逐字相同必须先短路命中"
    assert task_retrieval.same_goal(left, other) is False


def test_same_goal_without_an_embedder_falls_back_to_exact_text():
    left = {"title": "跟进回款", "content": "跟进项目回款。", "project": "P"}
    identical = {"title": "跟进回款", "content": "跟进项目回款。", "project": "P"}
    paraphrase = {"title": "完成回款", "content": "完成项目30%回款。", "project": "P"}
    assert task_retrieval.same_goal(left, identical) is True
    assert task_retrieval.same_goal(left, paraphrase) is False, "无向量时不得靠猜测放行"


def test_project_compatible_uses_resolved_family_over_names():
    class Source:
        def __init__(self, project, entity_id):
            self.item = {"project": project}
            self.project_entity_id = entity_id
            self.admission = {}

    lookup = {"红沙泉项目": "FAM-HSQ", "红沙泉二矿项目": "FAM-HSQ", "陶忽图项目": "FAM-THT"}
    task_retrieval.configure_semantics(FakeEmbedder(),
                                       family_lookup=lambda n: lookup.get(str(n or "").strip()))
    left = Source("红沙泉二矿项目", None)
    assert task_retrieval.project_compatible(left, {"project": "红沙泉项目"})
    assert not task_retrieval.project_compatible(left, {"project": "陶忽图项目"})
    # Two memory-resolved family ids that disagree are decisive.
    assert not task_retrieval.project_compatible(
        Source("红沙泉项目", "FAMILY-FAM-HSQ"), {"project": "红沙泉项目", "project_entity_id": "FAMILY-FAM-THT"})
    assert task_retrieval.project_compatible(
        Source("红沙泉项目", "FAMILY-FAM-HSQ"), {"project": None, "project_entity_id": "FAMILY-FAM-HSQ"})
    # A legacy id cannot be compared that way, so the resolved family decides —
    # otherwise every task written before the change would be unrecallable.
    assert task_retrieval.project_compatible(
        Source("红沙泉二矿项目", "FAMILY-FAM-HSQ"),
        {"project": "红沙泉项目", "project_entity_id": "PARENT-LEGACY"})


def test_no_project_on_either_side_is_compatible():
    class Source:
        item = {"project": None}
        project_entity_id = None
        admission = {}

    assert task_retrieval.project_compatible(Source(), {"project": None})
    assert not task_retrieval.project_compatible(Source(), {"project": "某具体项目"})


def test_goal_similarity_is_zero_without_an_embedder():
    assert task_retrieval.goal_similarity({"title": "a", "content": "b"},
                                          {"title": "a", "content": "b"}) == 0.0
