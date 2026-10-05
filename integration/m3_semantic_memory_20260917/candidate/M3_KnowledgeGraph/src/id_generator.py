"""M3 稳定节点身份生成。

原则：同一业务实体无论导入多少次、来自哪次会议，id 恒定 → MERGE 幂等。
- Project        ← 优先用 canonical 名归一化 hash；M2 entity_id 只作为 provenance 属性
                   （entity_id 是 per-meeting 的 P0001..，跨会议不稳定，不能当全局身份）
- ProjectAlias   ← project_id + alias 文本归一化
- Department     ← 归一化部门名
- WorkSection    ← 归一化部门名 + 归一化板块名
- DeliveryGroup  ← 归一化部门名 + 归一化交付组名（用户契约：name + department）
- Person         ← 归一化人名
- SourceDocument ← source_document_id（CLI 传入/文件名派生，稳定字符串）
- MeetingItem    ← sha1(source_document_id + 排序后的 merge_trace.source_indexes)
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_PREFIX = "m3"


def normalize_name(name: str) -> str:
    """归一化业务名称：Unicode NFKC、去首尾空白、压缩内部空白。不做同义判断（那是 M2 的事）。"""
    s = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def project_id(canonical_name: str) -> str:
    return f"project:{_PREFIX}:sha1:{_sha1('project|' + normalize_name(canonical_name))}"


def alias_id(project_node_id: str, alias: str) -> str:
    return f"alias:{_PREFIX}:sha1:{_sha1(project_node_id + '|' + normalize_name(alias))}"


def department_id(name: str) -> str:
    return f"dept:{_PREFIX}:sha1:{_sha1('dept|' + normalize_name(name))}"


def work_section_id(department: str | None, section: str) -> str:
    dep = normalize_name(department) if department else ""
    return f"section:{_PREFIX}:sha1:{_sha1('section|' + dep + '|' + normalize_name(section))}"


def delivery_group_id(department: str | None, group: str) -> str:
    dep = normalize_name(department) if department else ""
    return f"dg:{_PREFIX}:sha1:{_sha1('dg|' + dep + '|' + normalize_name(group))}"


def person_id(name: str) -> str:
    return f"person:{_PREFIX}:sha1:{_sha1('person|' + normalize_name(name))}"


def source_document_id(doc_id: str) -> str:
    # doc_id 本身已是稳定字符串（CLI 参数或文件名），仅做空白归一
    return f"doc:{_PREFIX}:{normalize_name(doc_id)}"


def meeting_item_id(doc_id: str, source_indexes: list[int]) -> str:
    key = doc_id + "#" + ",".join(str(i) for i in sorted(source_indexes))
    return f"item:{_PREFIX}:sha1:{_sha1(key)}"
