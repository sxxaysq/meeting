# -*- coding: utf-8 -*-
"""M2 归并服务命令行（与 service.py 共用 runner.py，业务逻辑只一份）。

用法::

    # 1. 在 MySQL m1_staging 库里建 ods_m2_* 四表四视图（幂等）
    M2_DSN='mysql://m1_dev:<密码>@192.168.30.216:3306/m1_staging' \\
        python cli.py bootstrap-schema

    # 2. 看还有哪些文档待归并
    python cli.py pending

    # 3. 全量初始化（按会议时间序，项目主表跨会议累积 alias）
    python cli.py consolidate-all --workers 4

    # 4. 增量：只跑从未归并 / M1 输入已变更的（顿悟工作流走的就是这个）
    python cli.py run-pending --limit 1 --workers 4

    # 5. 单份重跑与统计
    python cli.py consolidate --doc 2026-04-07 --force
    python cli.py stats --doc 2026-04-07

不给 --dsn 时读环境变量 M2_DSN；再不给就落本机 SQLite 自测库 data/m2_staging.db。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import runner  # noqa: E402  (导入时会把 M2 src 挂到 sys.path)
from repository import build_repository  # noqa: E402
from runner import M2Error  # noqa: E402

DEFAULT_DSN = "sqlite:///" + str(HERE / "data" / "m2_staging.db")


def _dump(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _repository(args):
    dsn = args.dsn or os.getenv("M2_DSN", DEFAULT_DSN)
    return build_repository(dsn, str(HERE))


def _guard_llm_env(args) -> None:
    """要调模型却没设 LLM_BASE_URL 时直接拦下，不让整批白跑。

    M2 的 llm_client 缺省指向 http://127.0.0.1:7060/v1，本机没这个服务，
    跑起来就是一片 Connection refused，而且每份文档都先跑几十秒才报错。
    """
    if getattr(args, "no_llm", False) or not getattr(args, "needs_llm", False):
        return
    if os.getenv("LLM_BASE_URL"):
        return
    raise SystemExit(
        "未设置 LLM_BASE_URL：M2 会退化到缺省的 http://127.0.0.1:7060/v1（本机无此服务）。\n"
        "两种正式跑法二选一：\n"
        "  1) 走已启的服务（start_m2_service.sh 已注入全部 LLM_* 变量）：\n"
        "     curl -X POST http://127.0.0.1:18093/m2/consolidate-all\n"
        "  2) 走 CLI，先导出：\n"
        "     export LLM_BASE_URL=http://192.168.30.215:8000/v1\n"
        "     export LLM_MODEL='Qwen/Qwen3.6-35B-A3B' LLM_TRANSPORT=openai\n"
        "     export LLM_TEMPERATURE=0 LLM_ENABLE_THINKING=false LLM_MAX_TOKENS=8192\n"
        "只验链路不调模型可加 --no-llm（判定全 UNCERTAIN，不会合并任何条目）。"
    )


def cmd_bootstrap_schema(args) -> int:
    repository = _repository(args)
    result = repository.bootstrap_schema()
    _dump({"dsn_scheme": (args.dsn or os.getenv("M2_DSN", DEFAULT_DSN)).split(":")[0], **result})
    return 0


def cmd_pending(args) -> int:
    repository = _repository(args)
    rows = repository.list_pending(limit=args.limit)
    _dump(
        {
            "m1_document_count": len(repository.list_documents()),
            "pending_count": len(rows),
            "pending": rows,
        }
    )
    return 0


def cmd_consolidate(args) -> int:
    repository = _repository(args)
    _dump(
        runner.consolidate_document(
            repository,
            args.doc,
            workers=args.workers,
            force=args.force,
            no_llm=args.no_llm,
            allow_invalid=args.allow_invalid,
        )
    )
    return 0


def cmd_run_pending(args) -> int:
    repository = _repository(args)
    _dump(
        runner.consolidate_pending(
            repository,
            limit=args.limit,
            workers=args.workers,
            no_llm=args.no_llm,
            allow_invalid=args.allow_invalid,
        )
    )
    return 0


def cmd_consolidate_all(args) -> int:
    repository = _repository(args)
    _dump(
        runner.consolidate_all(
            repository,
            workers=args.workers,
            force=args.force,
            no_llm=args.no_llm,
            allow_invalid=args.allow_invalid,
        )
    )
    return 0


def cmd_stats(args) -> int:
    repository = _repository(args)
    _dump(repository.stats(args.doc))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M2 语义归并：数据中台输入 → ods_m2_* 落库")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--dsn", default=None, help="缺省读环境变量 M2_DSN")
        p.add_argument("--workers", type=int, default=int(os.getenv("M2_WORKERS", "4")),
                       help="两两判定的并发数（只影响 LLM 调用并发）")
        p.add_argument("--no-llm", action="store_true",
                       help="不调模型（判定全部 UNCERTAIN，仅供链路自测）")
        p.add_argument("--allow-invalid", action="store_true",
                       help="质量门 ERROR 也强制落库（默认拒绝，不静默修正）")

    p = sub.add_parser("bootstrap-schema", help="建 ods_m2_* 四表四视图（幂等）")
    p.add_argument("--dsn", default=None)
    p.set_defaults(func=cmd_bootstrap_schema)

    p = sub.add_parser("pending", help="列出待归并文档")
    p.add_argument("--dsn", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("consolidate", help="归并指定的一份文档")
    common(p)
    p.add_argument("--doc", required=True, help="source_document_id")
    p.add_argument("--force", action="store_true", help="忽略输入指纹，强制重跑")
    p.set_defaults(func=cmd_consolidate, needs_llm=True)

    p = sub.add_parser("run-pending", help="增量归并（顿悟工作流走这个）")
    common(p)
    p.add_argument("--limit", type=int, default=1, help="本次最多处理几份，默认 1")
    p.set_defaults(func=cmd_run_pending, needs_llm=True)

    p = sub.add_parser("consolidate-all", help="全量归并（初始化 / 幂等回归）")
    common(p)
    p.add_argument("--force", action="store_true", help="忽略输入指纹，全部重跑")
    p.set_defaults(func=cmd_consolidate_all, needs_llm=True)

    p = sub.add_parser("stats", help="查一份文档的归并结果统计")
    p.add_argument("--dsn", default=None)
    p.add_argument("--doc", required=True)
    p.set_defaults(func=cmd_stats)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    _guard_llm_env(args)
    try:
        return args.func(args)
    except M2Error as error:
        # 不静默修正、不吞异常：失败原文与留档路径一起打到 stderr
        print(
            json.dumps(
                {"status_code": error.status_code, "detail": error.detail, **error.extra},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
