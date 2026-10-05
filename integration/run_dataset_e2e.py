#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据集端到端测试驱动：PDF → M1 抽取/入库 → M4 招投标分离 → M2 语义归并。

按用户口径**覆盖重灌**：doc id 用规范日期形态（2026-04-07 等），与库内现有 15 个
文档一一对应，重抽结果直接覆盖存量；不新增文档、不留孤儿。

可回滚：`M1_Extraction/out_v14/generic/*.items.json` 是存量原件，
需要时用 `curl -X POST :18090/m1/ingest-file -d '{"path":"..."}'` 逐份重灌即可。

用法::

    python run_dataset_e2e.py snapshot          # 只做覆盖前快照
    python run_dataset_e2e.py run               # 全量跑（约 35 分钟）
    python run_dataset_e2e.py run --only 2026-04-07,2026-04-13
    python run_dataset_e2e.py report            # 只看对比报告

各阶段结果落 /tmp/e2e_state.json，中断后可续跑（已成功的文档默认跳过，--force 重跑）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DATASET_DIR = Path("/home/yty-s/meeting-m2-work/数据集")
M1_EXTRACT = "http://127.0.0.1:18091/m1/extract"
M1_INGEST = "http://127.0.0.1:18090/m1/ingest"
M1_STATS = "http://127.0.0.1:18090/m1/stats"
M4_SPLIT = "http://127.0.0.1:8093/m4/split"
M4_STATS = "http://127.0.0.1:8093/m4/stats"
M2_CONSOLIDATE = "http://127.0.0.1:18093/m2/consolidate"
M2_CONSOLIDATE_ALL = "http://127.0.0.1:18093/m2/consolidate-all"
M2_PENDING = "http://127.0.0.1:18093/m2/pending"

STATE_PATH = Path("/tmp/e2e_state.json")
MODE = "generic"          # 与库内存量一致（ods_m1_source_documents.mode 全为 generic）
EXTRACT_TIMEOUT = 900     # 单份 generic 抽取实测 ~80s，留足余量

# 8.24 那份在库里的 doc id 是非日期形态（当年直接拿文件名当身份），
# 保持原样才能覆盖而不是新增第 16 个文档。
DOC_ID_OVERRIDES = {
    "2026.8.24信息公司周例会工作安排备忘录.pdf": "2026.8.24信息公司周例会工作安排备忘录",
}

# M4 侧不再需要手工映射：`m4_bidding` 已内置 doc id 归一
# （repository.resolve_source_document_id 向 ods_m1_source_documents 已登记的口径对齐），
# 传 M1 的 doc id 进去，服务端自己会处理。以前这里靠 M4_DOC_ID_OVERRIDES 硬映射
# 到 `2026-08-24`，反而造成 M4 的 origin_item_id JOIN 不回 M1。


def doc_id_and_date(pdf: Path):
    """从 `2026.4.7信息公司周例会工作安排备忘录.pdf` 解析出规范 doc id 与会议日期。"""
    if pdf.name in DOC_ID_OVERRIDES:
        doc_id = DOC_ID_OVERRIDES[pdf.name]
    else:
        match = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", pdf.name)
        if not match:
            raise SystemExit("文件名里没有 YYYY.M.D 日期，无法确定 doc id：{}".format(pdf.name))
        year, month, day = match.groups()
        doc_id = "{:04d}-{:02d}-{:02d}".format(int(year), int(month), int(day))
    # 会议日期：doc id 形如 YYYY-MM-DD 才解析，非日期形态不猜测（与流水线约定一致）
    meeting_date = doc_id if re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc_id) else None
    if meeting_date is None:
        match = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", pdf.name)
        meeting_date = "{:04d}-{:02d}-{:02d}".format(*[int(g) for g in match.groups()])
    return doc_id, meeting_date


def list_pdfs():
    pdfs = sorted(DATASET_DIR.glob("*.pdf"))
    if not pdfs:
        raise SystemExit("数据集目录没有 PDF：{}".format(DATASET_DIR))
    return pdfs


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"snapshot": {}, "docs": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def post_json(url: str, payload: dict, timeout: int = 300) -> dict:
    request = urllib.request.Request(
        url,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        {"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return {"status": resp.status, "body": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw[:500]}
        return {"status": error.code, "body": body}


def get_json(url: str, timeout: int = 120) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_multipart(url: str, fields: dict, file_path: Path, timeout: int) -> dict:
    """手写 multipart/form-data：m1_service 的 /m1/extract 收的是 UploadFile，
    必须真发文件分片（顿悟 HTTP 节点就是因为发不出 multipart 才阻断的）。"""
    boundary = "----e2e" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(
            "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(
                boundary, key, value
            ).encode("utf-8")
        )
    filename = file_path.name.encode("utf-8").decode("utf-8")
    parts.append(
        "--{}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{}\"\r\n"
        "Content-Type: application/pdf\r\n\r\n".format(boundary, filename).encode("utf-8")
    )
    parts.append(file_path.read_bytes())
    parts.append("\r\n--{}--\r\n".format(boundary).encode("utf-8"))
    body = b"".join(parts)
    request = urllib.request.Request(
        url, body, {"Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST"
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return {
                "status": resp.status,
                "body": json.loads(resp.read().decode("utf-8")),
                "seconds": round(time.monotonic() - started, 1),
            }
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:500]}
        return {"status": error.code, "body": parsed, "seconds": round(time.monotonic() - started, 1)}


# --------------------------------------------------------------------------
# 阶段 0：覆盖前快照
# --------------------------------------------------------------------------


def cmd_snapshot() -> int:
    state = load_state()
    print("快照覆盖前的库内现状（逐文档）...")
    for pdf in list_pdfs():
        doc_id, meeting_date = doc_id_and_date(pdf)
        try:
            m1 = get_json("{}?source_document_id={}".format(M1_STATS, urllib.parse.quote(doc_id)))
        except Exception as error:  # noqa: BLE001
            m1 = {"error": str(error)}
        # M4 侧用同一个 doc id；服务端会自行归一到 ods_m1_source_documents 的口径
        m4_doc_id = doc_id
        try:
            m4 = get_json("{}?source_document_id={}".format(M4_STATS, urllib.parse.quote(m4_doc_id)))
        except Exception as error:  # noqa: BLE001
            m4 = {"error": str(error)}
        state["snapshot"][doc_id] = {
            "pdf": pdf.name,
            "meeting_date": meeting_date,
            "m4_doc_id": m4_doc_id,
            "m1_item_count": (m1.get("item_count") if isinstance(m1, dict) else None),
            "m1_document": m1.get("document") if isinstance(m1, dict) else None,
            "m4_bidding_count": (m4.get("bidding_count") if isinstance(m4, dict) else None),
        }
        print("  {:<44} m1={:<5} m4_bidding={}".format(
            doc_id, state["snapshot"][doc_id]["m1_item_count"],
            state["snapshot"][doc_id]["m4_bidding_count"]))
    save_state(state)
    print("快照已写入 {}".format(STATE_PATH))
    return 0


# --------------------------------------------------------------------------
# 阶段 1-2：逐份 PDF 跑 M1 抽取/入库 + M4 分离
# --------------------------------------------------------------------------


def run_one(pdf: Path, state: dict, force: bool) -> dict:
    doc_id, meeting_date = doc_id_and_date(pdf)
    record = state["docs"].setdefault(doc_id, {"pdf": pdf.name, "meeting_date": meeting_date})
    if record.get("m4_ok") and not force:
        print("  = {} 已跑过，跳过（--force 可重跑）".format(doc_id))
        return record

    # ---- M1 抽取 ----
    print("  [M1 extract] {} ({:.0f} KB)".format(pdf.name, pdf.stat().st_size / 1024))
    result = post_multipart(M1_EXTRACT, {"mode": MODE}, pdf, EXTRACT_TIMEOUT)
    record["m1_extract_status"] = result["status"]
    record["m1_extract_seconds"] = result.get("seconds")
    if result["status"] != 200:
        record["m1_ok"] = False
        record["m1_error"] = json.dumps(result["body"], ensure_ascii=False)[:600]
        print("      !! HTTP {} {}".format(result["status"], record["m1_error"][:200]))
        return record
    items = result["body"]["items"]
    record["m1_ok"] = True
    record["m1_item_count"] = len(items)
    exact = sum(1 for it in items if (it.get("evidence") or {}).get("exact_match"))
    record["m1_exact_match"] = exact
    record["m1_exact_rate"] = round(exact / max(1, len(items)), 4)
    print("      -> {} 条，exact_match {:.1%}，{} s".format(
        len(items), record["m1_exact_rate"], record["m1_extract_seconds"]))

    # ---- M1 入库（覆盖重灌）----
    ingest = post_json(M1_INGEST, {
        "source_document_id": doc_id,
        "file_name": pdf.name,
        "meeting_date": meeting_date,
        "mode": MODE,
        "items": items,
    })
    record["m1_ingest_status"] = ingest["status"]
    if ingest["status"] != 200:
        record["m1_ingest_ok"] = False
        record["m1_ingest_error"] = json.dumps(ingest["body"], ensure_ascii=False)[:600]
        print("      !! ingest HTTP {} {}".format(ingest["status"], record["m1_ingest_error"][:200]))
        return record
    record["m1_ingest_ok"] = True
    record["m1_ingest_item_count"] = ingest["body"].get("item_count")
    print("      -> ingest ok，库内 {} 条".format(record["m1_ingest_item_count"]))

    # ---- M4 招投标分离 ----
    split = post_json(M4_SPLIT, {
        "source_document_id": doc_id,
        "file_name": pdf.name,
        "meeting_date": meeting_date,
        "mode": MODE,
        "items": items,
    }, timeout=900)
    record["m4_doc_id"] = doc_id
    record["m4_status"] = split["status"]
    if split["status"] != 200:
        record["m4_ok"] = False
        record["m4_error"] = json.dumps(split["body"], ensure_ascii=False)[:600]
        print("      !! m4 HTTP {} {}".format(split["status"], record["m4_error"][:200]))
        return record
    body = split["body"]
    record["m4_ok"] = True
    record["m4_bidding_count"] = body.get("bidding_count")
    record["m4_filtered_count"] = body.get("filtered_count")
    record["m4_disagreements"] = len(body.get("disagreements") or [])
    record["m4_llm_calls"] = (body.get("llm_stats") or {}).get("calls")
    print("      -> m4 招投标 {} 条 / 过滤 {} 条，正则分歧 {} 处".format(
        record["m4_bidding_count"], record["m4_filtered_count"], record["m4_disagreements"]))
    return record


def cmd_run(only, force) -> int:
    state = load_state()
    if not state.get("snapshot"):
        print("先做覆盖前快照...")
        cmd_snapshot()
        state = load_state()

    pdfs = list_pdfs()
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        pdfs = [p for p in pdfs if doc_id_and_date(p)[0] in wanted]
        if not pdfs:
            raise SystemExit("--only 没匹配到任何 PDF：{}".format(only))

    print("\n===== 阶段 1-2：M1 抽取/入库 + M4 分离（{} 份）=====".format(len(pdfs)))
    started = time.monotonic()
    for index, pdf in enumerate(pdfs, 1):
        print("\n[{}/{}]".format(index, len(pdfs)))
        try:
            run_one(pdf, state, force)
        except Exception as error:  # noqa: BLE001 - 单份失败不阻断整批，但要如实记录
            doc_id = doc_id_and_date(pdf)[0]
            state["docs"].setdefault(doc_id, {"pdf": pdf.name})["fatal"] = str(error)[:400]
            print("      !! 异常：{}".format(str(error)[:300]))
        save_state(state)
    print("\n阶段 1-2 用时 {:.1f} 分钟".format((time.monotonic() - started) / 60))

    print("\n===== 阶段 3：M2 语义归并（按指纹增量，输入变了的才重跑）=====")
    pending = get_json(M2_PENDING)
    print("  待归并 {} 份".format(pending["pending_count"]))
    started = time.monotonic()
    result = post_json(M2_CONSOLIDATE_ALL, {}, timeout=3600)
    state["m2"] = {"status": result["status"], "body": result["body"],
                   "seconds": round(time.monotonic() - started, 1)}
    save_state(state)
    body = result["body"]
    if result["status"] == 200:
        print("  -> processed={} skipped={} failed={} pending_left={} 用时 {:.1f}s".format(
            body.get("processed"), body.get("skipped"), body.get("failed"),
            body.get("pending_left"), state["m2"]["seconds"]))
    else:
        print("  !! HTTP {} {}".format(result["status"], json.dumps(body, ensure_ascii=False)[:600]))
    return 0 if result["status"] == 200 else 1


# --------------------------------------------------------------------------
# 阶段 4：对比报告
# --------------------------------------------------------------------------


def cmd_report() -> int:
    state = load_state()
    snapshot = state.get("snapshot") or {}
    docs = state.get("docs") or {}
    if not docs:
        print("还没有跑过，先执行 run。")
        return 1

    print("=" * 118)
    print("{:<44} {:>7} {:>7} {:>6} {:>8} {:>7} {:>7} {:>7}".format(
        "doc_id", "旧m1", "新m1", "差", "exact", "旧m4", "新m4", "M2"))
    print("-" * 118)
    totals = {"old_m1": 0, "new_m1": 0, "old_m4": 0, "new_m4": 0, "m2_ok": 0, "m2_items": 0}
    problems = []
    for doc_id in sorted(docs):
        row = docs[doc_id]
        old = snapshot.get(doc_id, {})
        old_m1, new_m1 = old.get("m1_item_count"), row.get("m1_item_count")
        old_m4, new_m4 = old.get("m4_bidding_count"), row.get("m4_bidding_count")
        diff = (new_m1 - old_m1) if isinstance(new_m1, int) and isinstance(old_m1, int) else None
        totals["old_m1"] += old_m1 or 0
        totals["new_m1"] += new_m1 or 0
        totals["old_m4"] += old_m4 or 0
        totals["new_m4"] += new_m4 or 0
        if not row.get("m1_ok"):
            problems.append("{}: M1 抽取失败 {}".format(doc_id, row.get("m1_error", "")))
        if not row.get("m1_ingest_ok"):
            problems.append("{}: M1 入库失败 {}".format(doc_id, row.get("m1_ingest_error", "")))
        if not row.get("m4_ok"):
            problems.append("{}: M4 分离失败 {}".format(doc_id, row.get("m4_error", "")))
        if row.get("fatal"):
            problems.append("{}: 异常 {}".format(doc_id, row["fatal"]))
        print("{:<44} {:>7} {:>7} {:>6} {:>8} {:>7} {:>7} {:>7}".format(
            doc_id[:44],
            old_m1 if old_m1 is not None else "-",
            new_m1 if new_m1 is not None else "-",
            "{:+d}".format(diff) if diff is not None else "-",
            "{:.1%}".format(row["m1_exact_rate"]) if row.get("m1_exact_rate") is not None else "-",
            old_m4 if old_m4 is not None else "-",
            new_m4 if new_m4 is not None else "-",
            "ok" if row.get("m4_ok") else "FAIL",
        ))
    print("-" * 118)
    print("{:<44} {:>7} {:>7} {:>6} {:>8} {:>7} {:>7}".format(
        "合计", totals["old_m1"], totals["new_m1"],
        "{:+d}".format(totals["new_m1"] - totals["old_m1"]), "",
        totals["old_m4"], totals["new_m4"]))

    m2 = state.get("m2") or {}
    if m2:
        body = m2.get("body") or {}
        print("\nM2：HTTP {} processed={} skipped={} failed={} pending_left={} 用时 {}s".format(
            m2.get("status"), body.get("processed"), body.get("skipped"),
            body.get("failed"), body.get("pending_left"), m2.get("seconds")))
        for row in (body.get("documents") or [])[:20]:
            print("   {:<44} m1={:<5} m2={:<5} merge={:<3} {}".format(
                str(row.get("source_document_id"))[:44], row.get("m1_item_count"),
                row.get("item_count"), row.get("merge_operations"), row.get("validation_status")))
        for row in (body.get("failures") or []):
            problems.append("{}: M2 失败 {}".format(
                row.get("source_document_id"), str(row.get("detail"))[:200]))

    print("\n===== 问题清单 =====")
    if problems:
        for line in problems:
            print("  ! " + line)
    else:
        print("  无")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="数据集端到端测试：M1 → M4 → M2")
    parser.add_argument("command", choices=("snapshot", "run", "report"))
    parser.add_argument("--only", default=None, help="只跑指定 doc id，逗号分隔")
    parser.add_argument("--force", action="store_true", help="已跑过的也重跑")
    args = parser.parse_args()
    if args.command == "snapshot":
        return cmd_snapshot()
    if args.command == "run":
        return cmd_run(args.only, args.force)
    return cmd_report()


if __name__ == "__main__":
    sys.exit(main())
