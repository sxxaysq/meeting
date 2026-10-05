#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从数据中台的 ods_m1_* 存量重跑 M4 招投标分离。

用途：M4 的 `upsert_bidding` 早期只 upsert 不清理，重分类后上一轮判为招投标、
这一轮没判中的行会残留（条数还可能巧合相等而看不出来）。修复后需要把存量
重刷一遍才能得到自洽的 `ods_m4_bidding_items`。

输入取自 `ods_m1_meeting_items.item_json`（就是 M1 的输出，与 M2 的取数口径一致），
**只 SELECT**，不改 ods_m1_*。

用法::

    python rerun_m4_from_staging.py            # 全部文档
    python rerun_m4_from_staging.py --only 2026-04-20,2026-05-25
    python rerun_m4_from_staging.py --dry      # 只读，不调 M4
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import pymysql

M4_SPLIT = "http://127.0.0.1:8093/m4/split"

# 不再需要手工映射 doc id：`m4_bidding` 已内置归一
# （repository.resolve_source_document_id 向 ods_m1_source_documents 已登记的口径对齐）。
# 本脚本的输入本来就取自 ods_m1_*，doc id 天然就是 M1 口径，直接透传即可。


def connect():
    dsn = os.environ.get("M2_DSN") or os.environ.get("M1_STAGING_DSN")
    if not dsn:
        raise SystemExit("需要 M2_DSN 或 M1_STAGING_DSN 环境变量（mysql://user:YOUR_PASSWORD@host:port/db）")
    import urllib.parse

    p = urllib.parse.urlsplit(dsn)
    # autocommit=True：避免长事务在 REPEATABLE READ 下读到冻结快照
    return pymysql.connect(
        host=p.hostname, port=p.port or 3306,
        user=p.username, password=p.password,
        database=(p.path or "/").lstrip("/"), charset="utf8mb4", autocommit=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="从中台存量重跑 M4 分离")
    parser.add_argument("--only", default=None, help="只跑指定 doc id，逗号分隔")
    parser.add_argument("--dry", action="store_true", help="只读现状，不调 M4")
    args = parser.parse_args()

    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT source_document_id, file_name, meeting_date, mode "
        "FROM ods_m1_source_documents ORDER BY source_document_id"
    )
    docs = cur.fetchall()
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        docs = [d for d in docs if d[0] in wanted]

    print("{:<44} {:>6} {:>8} {:>10} {:>8} {:>9}".format(
        "doc_id", "m1条", "旧m4行", "新bidding", "filtered", "耗时s"))
    print("-" * 90)
    total_old = total_new = failures = 0
    for doc_id, file_name, meeting_date, mode in docs:
        m4_id = doc_id
        cur.execute(
            "SELECT item_json FROM ods_m1_meeting_items "
            "WHERE source_document_id = %s ORDER BY item_seq", (doc_id,)
        )
        items = [json.loads(row[0]) for row in cur.fetchall()]
        cur.execute(
            "SELECT COUNT(*) FROM ods_m4_bidding_items WHERE source_document_id = %s", (m4_id,)
        )
        old_rows = cur.fetchone()[0]
        total_old += old_rows
        if args.dry:
            print("{:<44} {:>6} {:>8} {:>10} {:>8} {:>9}".format(
                str(doc_id)[:44], len(items), old_rows, "-", "-", "-"))
            continue

        payload = {
            "source_document_id": m4_id,
            "file_name": file_name,
            "meeting_date": str(meeting_date) if meeting_date else None,
            "mode": mode,
            "items": items,
        }
        started = time.monotonic()
        request = urllib.request.Request(
            M4_SPLIT,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            {"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            new = body.get("bidding_count")
            filtered = body.get("filtered_count")
            total_new += new or 0
        except (urllib.error.URLError, urllib.error.HTTPError) as error:
            detail = error.read().decode("utf-8", "replace")[:200] if hasattr(error, "read") else str(error)
            new = filtered = "FAIL"
            failures += 1
            print("    !! {}".format(detail))
        print("{:<44} {:>6} {:>8} {:>10} {:>8} {:>9.1f}".format(
            str(doc_id)[:44], len(items), old_rows, str(new), str(filtered),
            time.monotonic() - started))

    print("-" * 90)
    print("{:<44} {:>6} {:>8} {:>10}".format("合计", "", total_old, total_new))
    if failures:
        print("失败 {} 份".format(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
