#!/usr/bin/env bash
# 增量抽取新增 4 份会议（2026-07-20/07-27/08-10/08-17），generic 模式与既有 10 份一致。
# 产物：M1_Extraction/out_v14/generic/<YYYY-MM-DD>.items.json（+ .report.json）
# 注：2026-08-20 清理后最新全量 14 份归并于 out_v14；本脚本 OUT 指向同一目录，重跑即覆盖。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${MEETING_INPUT_DIR:?Set MEETING_INPUT_DIR to the source PDF directory}"
OUT="${M1_OUTPUT_DIR:-$REPO_ROOT/M1_Extraction/out_v14/generic}"
mkdir -p "$OUT"

export LLM_BASE_URL=http://192.168.30.215:8000/v1
export LLM_MODEL="Qwen/Qwen3.6-35B-A3B"
export LLM_ENABLE_THINKING=false
export LLM_TEMPERATURE=0
export LLM_MAX_TOKENS=65536   # generic 整篇单调用，输出可达 2w+ tokens，8192 会截断

PY="${PYTHON_BIN:-python}"
CLI="$REPO_ROOT/M1_Extraction/src/cli.py"

run_one() {  # $1=文件名前缀(2026.7.20)  $2=规范日期(2026-07-20)
  echo "=== [$(date +%H:%M:%S)] 开始 $1 -> $2"
  "$PY" "$CLI" extract "$SRC/$1信息公司周例会工作安排备忘录.pdf" \
    -o "$OUT/$2.items.json" --mode generic --workers 2
  echo "=== [$(date +%H:%M:%S)] 完成 $2"
}

run_one "2026.7.20" "2026-07-20"
run_one "2026.7.27" "2026-07-27"
run_one "2026.8.10" "2026-08-10"
run_one "2026.8.17" "2026-08-17"

echo ALL_DONE
