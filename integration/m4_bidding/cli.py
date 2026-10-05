# -*- coding: utf-8 -*-
"""M4 bidding CLI：对 m1 产出的 items.json 做招投标分类切分。

用法::

    export M4_BIDDING_DSN='mysql://m1_dev:<密码>@192.168.30.216:3306/m1_staging'
    python cli.py split --input M1_Extraction/out_v14/generic/2026-04-07.items.json
    python cli.py split-dir --dir M1_Extraction/out_v14/generic   # 增量（sha1 未变跳过）
    python cli.py stats --doc 2026-04-07

产物（默认在 m4_bidding/data/ 下）：
    bidding/<doc_id>.bidding.json    招投标条目 + 分类注记（后续单独进 m3/m6）
    filtered/<doc_id>.items.json     m1 九字段过滤结果（替代原始输出供 m1 原下游）
    reports/<doc_id>.split_report.json  分类统计、正则分歧清单、LLM 统计

红线：不修改任何 m1 既有文件；招投标条目从 filtered 中剔除，m1 原始产物保持贴源不动。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from classifier import ClassificationError, classify_items, cross_check, split_items  # noqa: E402
from repository import (  # noqa: E402
    build_repository,
    date_from_doc_id,
    doc_id_from_path,
    meeting_date_from_doc_id,
)

M1_ROOT = HERE.parents[1] / "M1_Extraction"
sys.path.insert(0, str(M1_ROOT / "src"))

from llm_client import LLMClient, LLMConfig, LLMError  # noqa: E402

DEFAULT_DSN = "sqlite:///" + str(HERE / "data" / "m4_bidding.db")
STATE_PATH = HERE / "data" / "scan_state.json"
PATTERNS = ("*.items.json", "*.m1.json")


def _ensure_dirs() -> dict:
    dirs = {
        "bidding": HERE / "data" / "bidding",
        "filtered": HERE / "data" / "filtered",
        "reports": HERE / "data" / "reports",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _post_ingest(url: str, payload: dict) -> dict:
    """把 filtered 结果直推 m1_staging /m1/ingest（正式集成时下游切换用）。

    本次试验阶段不传 --ingest-url 则不触发；staging 存量数据不动。"""
    import urllib.request

    request = urllib.request.Request(
        url,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        {"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def process_items(
    items: list,
    client: LLMClient,
    repository,
    source_document_id: str,
    file_name: str,
    meeting_date: Optional[str],
    mode: str,
    out_dirs: dict,
    chunk_size: int,
    ingest_url: Optional[str] = None,
) -> dict:
    """单文档完整处理：分类 → 切分 → 招投标入表 → 三路产物落盘。"""
    # doc id 向 M1 已登记的口径归一（与 service.py 同一个 choke point 语义）：
    # 归一后的 id 才会用于派生 origin_item_id、写表、命名三路产物，
    # 保证 CLI 与 HTTP 两条入口落到同一个身份上。
    requested_id = source_document_id
    source_document_id = repository.resolve_source_document_id(source_document_id)
    if source_document_id != requested_id:
        print(
            "[split] doc id 归一：{} -> {}（对齐 ods_m1_source_documents）".format(
                requested_id, source_document_id
            )
        )
    result = classify_items(items, client, chunk_size=chunk_size)
    annotations = result["annotations"]
    split = split_items(items, annotations)
    disagreements = cross_check(items, annotations)
    repository.upsert_bidding(
        source_document_id=source_document_id,
        file_name=file_name,
        # 先严格 YYYY-MM-DD，再从文件名/doc id 的 YYYY.M.D 形态确定性取日期；
        # 都提不出来才置 NULL，不猜。
        meeting_date=meeting_date
        or meeting_date_from_doc_id(source_document_id)
        or date_from_doc_id(file_name)
        or date_from_doc_id(source_document_id),
        mode=mode,
        items=items,
        annotations={idx: annotations[idx] for idx in result["bidding_indices"]},
    )
    _write_json(
        out_dirs["bidding"] / "{}.bidding.json".format(source_document_id),
        {
            "source_document_id": source_document_id,
            "bidding_count": len(split["bidding"]),
            "bidding": split["bidding"],
        },
    )
    _write_json(
        out_dirs["filtered"] / "{}.items.json".format(source_document_id),
        {"items": split["filtered"]},
    )
    report = {
        "source_document_id": source_document_id,
        "file_name": file_name,
        "item_count": len(items),
        "bidding_count": len(split["bidding"]),
        "filtered_count": len(split["filtered"]),
        "by_category": {},
        "disagreements": disagreements,
        "llm_stats": result["llm_stats"],
    }
    for entry in split["bidding"]:
        report["by_category"][entry["bidding_category"]] = (
            report["by_category"].get(entry["bidding_category"], 0) + 1
        )
    _write_json(
        out_dirs["reports"] / "{}.split_report.json".format(source_document_id), report
    )
    if ingest_url:
        # 可选：把 filtered 直推 m1_staging /m1/ingest（正式集成时的下游切换开关）。
        report["ingest"] = _post_ingest(
            ingest_url,
            {
                "source_document_id": source_document_id,
                "file_name": file_name,
                "meeting_date": meeting_date
                or meeting_date_from_doc_id(source_document_id),
                "mode": mode,
                "items": split["filtered"],
            },
        )
    return report


def _build_client() -> LLMClient:
    return LLMClient(LLMConfig.from_env())


def _resolve_repository(dsn: Optional[str]):
    return build_repository(dsn or os.getenv("M4_BIDDING_DSN", DEFAULT_DSN), str(HERE))


def _validate_m1_payload(payload: dict, path: Path) -> list:
    """m1 九字段 Schema 严格校验（与 m1_staging 同一 schema 文件，失败即拒绝）。"""
    import jsonschema

    schema_path = M1_ROOT / "schemas" / "m1_items.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        jsonschema.Draft7Validator(schema).iter_errors(payload),
        key=lambda e: [str(p) for p in e.path],
    )
    if errors:
        messages = [
            "{}: {}".format("/".join(str(p) for p in e.path) or "$", e.message)
            for e in errors[:20]
        ]
        raise ClassificationError(
            "{} 不是合法 m1 输出（{}）：%s".format(path, messages[0]) % "; ".join(messages[:5])
        )
    return payload["items"]


def load_items(path: Path, validate: bool) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("{} 不是 {{\"items\": [...]}} 形态".format(path))
    if validate:
        return _validate_m1_payload(payload, path)
    return payload["items"]


def cmd_split(args) -> int:
    path = Path(args.input)
    if not path.exists() or not path.is_file():
        print("文件不存在：{}".format(path), file=sys.stderr)
        return 1
    items = load_items(path, validate=not args.no_validate)
    client = _build_client()
    repository = _resolve_repository(args.dsn)
    try:
        report = process_items(
            items=items,
            client=client,
            repository=repository,
            source_document_id=args.doc_id or doc_id_from_path(path),
            file_name=path.name,
            meeting_date=args.meeting_date,
            mode=args.mode,
            out_dirs=_ensure_dirs(),
            chunk_size=args.chunk_size,
            ingest_url=args.ingest_url,
        )
    finally:
        repository.close()
    print(
        "[split] {} items={} bidding={} filtered={} disagreements={}".format(
            report["source_document_id"], report["item_count"], report["bidding_count"],
            report["filtered_count"], len(report["disagreements"]),
        )
    )
    return 0


def cmd_split_dir(args) -> int:
    out_dir = Path(args.dir)
    if not out_dir.is_dir():
        print("目录不存在：{}".format(out_dir), file=sys.stderr)
        return 1
    files = sorted(
        {p for pattern in PATTERNS for p in out_dir.glob(pattern) if p.is_file()}
    )
    state_path = Path(args.state) if args.state else STATE_PATH
    state = _load_state(state_path)
    client = _build_client()
    repository = _resolve_repository(args.dsn)
    out_dirs = _ensure_dirs()
    done, skipped, failed = [], [], []
    try:
        for path in files:
            digest = hashlib.sha1(path.read_bytes()).hexdigest()
            key = str(path)
            if state.get(key, {}).get("sha1") == digest:
                skipped.append(key)
                continue
            try:
                items = load_items(path, validate=not args.no_validate)
                report = process_items(
                    items=items,
                    client=client,
                    repository=repository,
                    source_document_id=doc_id_from_path(path),
                    file_name=path.name,
                    meeting_date=args.meeting_date,
                    mode=args.mode,
                    out_dirs=out_dirs,
                    chunk_size=args.chunk_size,
                    ingest_url=args.ingest_url,
                )
            except Exception as error:  # noqa: BLE001 - 单文件失败不阻塞其余文件
                failed.append({"path": key, "error": str(error)})
                continue
            state[key] = {"sha1": digest, "bidding_count": report["bidding_count"]}
            done.append(key)
            print(
                "[split-dir] {} items={} bidding={} filtered={} disagreements={}".format(
                    report["source_document_id"], report["item_count"],
                    report["bidding_count"], report["filtered_count"],
                    len(report["disagreements"]),
                )
            )
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    finally:
        repository.close()
    print(
        "[split-dir] done={} skipped={} failed={}".format(len(done), len(skipped), len(failed))
    )
    for item in failed:
        print("  失败：{} -> {}".format(item["path"], item["error"]), file=sys.stderr)
    return 1 if failed else 0


def cmd_stats(args) -> int:
    repository = _resolve_repository(args.dsn)
    try:
        # 查询侧也归一：传 2026-08-24 与传 M1 登记的文件名形态得到同一份结果
        resolved = repository.resolve_source_document_id(args.doc)
        result = repository.stats(resolved)
        if resolved != args.doc:
            result["requested_source_document_id"] = args.doc
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        repository.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="M4 招投标分离：m1 条目 LLM 分类切分")
    sub = parser.add_subparsers(dest="command", required=True)

    p_split = sub.add_parser("split", help="单文件分类切分")
    p_split.add_argument("--input", required=True, help="m1 items.json 路径")
    p_split.add_argument("--doc-id", default=None, help="缺省用文件名去后缀")
    p_split.add_argument("--dsn", default=None, help="缺省读环境变量 M4_BIDDING_DSN")
    p_split.add_argument("--mode", default="generic")
    p_split.add_argument("--meeting-date", default=None)
    p_split.add_argument("--chunk-size", type=int, default=80)
    p_split.add_argument("--ingest-url", default=None,
                         help="可选：filtered 直推 m1_staging /m1/ingest（如 http://127.0.0.1:8090/m1/ingest）")
    p_split.add_argument("--no-validate", action="store_true", help="跳过 m1 九字段 Schema 校验")
    p_split.set_defaults(func=cmd_split)

    p_dir = sub.add_parser("split-dir", help="目录批处理（sha1 未变跳过，增量）")
    p_dir.add_argument("--dir", required=True)
    p_dir.add_argument("--dsn", default=None)
    p_dir.add_argument("--mode", default="generic")
    p_dir.add_argument("--meeting-date", default=None)
    p_dir.add_argument("--chunk-size", type=int, default=80)
    p_dir.add_argument("--ingest-url", default=None,
                       help="可选：filtered 直推 m1_staging /m1/ingest（如 http://127.0.0.1:8090/m1/ingest）")
    p_dir.add_argument("--state", default=None, help="缺省 data/scan_state.json")
    p_dir.add_argument("--no-validate", action="store_true")
    p_dir.set_defaults(func=cmd_split_dir)

    p_stats = sub.add_parser("stats", help="按文档查询招投标入库统计")
    p_stats.add_argument("--doc", required=True)
    p_stats.add_argument("--dsn", default=None)
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ClassificationError as error:
        print("分类失败（不静默修正）：{}".format(error), file=sys.stderr)
        raise SystemExit(2)
    except LLMError as error:
        print("LLM 调用失败：{}".format(error), file=sys.stderr)
        raise SystemExit(3)
