# -*- coding: utf-8 -*-
"""读取 PDF / DOCX / TXT，输出带页码字符区间的原始文本。

只做确定性抽取，不做任何清洗。清洗交给 text_normalizer，
这样 raw 偏移量始终可以回溯到 PDF 页。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class PageSpan:
    """raw_text 中属于某一页的字符区间，end 为开区间。"""

    page: int
    start: int
    end: int


@dataclass
class RawDocument:
    source_name: str
    raw_text: str
    page_spans: List[PageSpan] = field(default_factory=list)

    def page_of(self, offset: int) -> Optional[int]:
        for span in self.page_spans:
            if span.start <= offset < span.end:
                return span.page
        if self.page_spans and offset >= self.page_spans[-1].end:
            return self.page_spans[-1].page
        return None

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name,
            "raw_text": self.raw_text,
            "page_spans": [
                {"page": s.page, "start": s.start, "end": s.end} for s in self.page_spans
            ],
        }


def _read_pdf(path: Path) -> RawDocument:
    try:
        import pymupdf as fitz
    except ImportError:  # 兼容旧版包名
        try:
            import fitz
        except ImportError as error:  # pragma: no cover - 环境缺失时才触发
            raise RuntimeError("读取 PDF 需要 PyMuPDF：pip install pymupdf") from error

    parts: List[str] = []
    spans: List[PageSpan] = []
    cursor = 0
    document = fitz.open(path)
    try:
        for index, page in enumerate(document, start=1):
            text = page.get_text()
            # 与 pdftotext 一致：每页末尾补一个换页边界，避免跨页粘连。
            if text and not text.endswith("\n"):
                text += "\n"
            parts.append(text)
            spans.append(PageSpan(page=index, start=cursor, end=cursor + len(text)))
            cursor += len(text)
    finally:
        document.close()
    return RawDocument(source_name=path.name, raw_text="".join(parts), page_spans=spans)


def _read_docx(path: Path) -> RawDocument:
    try:
        from docx import Document
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("读取 DOCX 需要 python-docx：pip install python-docx") from error

    paragraphs = [p.text for p in Document(str(path)).paragraphs]
    text = "\n".join(paragraphs)
    # DOCX 没有可靠的物理分页信息，页码留空由 evidence 对齐时置 null。
    return RawDocument(source_name=path.name, raw_text=text, page_spans=[])


def _read_txt(path: Path) -> RawDocument:
    text = path.read_text(encoding="utf-8")
    return RawDocument(source_name=path.name, raw_text=text, page_spans=[])


def _read_json_snapshot(path: Path) -> RawDocument:
    """支持 Dify Document Extractor 导出的 {"raw_text":..., "page_spans":[...]}。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    spans = [
        PageSpan(page=int(s["page"]), start=int(s["start"]), end=int(s["end"]))
        for s in payload.get("page_spans", [])
    ]
    return RawDocument(
        source_name=payload.get("source_name", path.name),
        raw_text=payload["raw_text"],
        page_spans=spans,
    )


READERS = {
    ".pdf": _read_pdf,
    ".docx": _read_docx,
    ".txt": _read_txt,
    ".json": _read_json_snapshot,
}


def read_document(path) -> RawDocument:
    path = Path(path)
    if not path.is_file():
        raise RuntimeError("输入文件不存在：{}".format(path))
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        raise RuntimeError("仅支持 .pdf / .docx / .txt / .json，收到：{}".format(path.suffix))
    return reader(path)
