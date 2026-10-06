#!/usr/bin/env bash
# 补跑新增 4 份会议的 block（正则结构切分）模式，与 out_eval_block_0820 同模式。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${MEETING_INPUT_DIR:?Set MEETING_INPUT_DIR to the source PDF directory}"
OUT="${M1_OUTPUT_DIR:-$REPO_ROOT/M1_Extraction/out_v14/block}"
mkdir -p "$OUT"
export LLM_BASE_URL=http://192.168.30.215:8000/v1
export LLM_MODEL="Qwen/Qwen3.6-35B-A3B"
export LLM_ENABLE_THINKING=false
export LLM_TEMPERATURE=0
export LLM_MAX_TOKENS=65536
PY="${PYTHON_BIN:-python}"
CLI="$REPO_ROOT/M1_Extraction/src/cli.py"
run_one() {
  echo "=== [$(date +%H:%M:%S)] 开始 $2 (block)"
  "$PY" "$CLI" extract "$SRC/$1信息公司周例会工作安排备忘录.pdf" \
    -o "$OUT/$2.items.json" --mode block --workers 2
}
run_one "2026.7.20" "2026-07-20"
run_one "2026.7.27" "2026-07-27"
run_one "2026.8.10" "2026-08-10"
run_one "2026.8.17" "2026-08-17"
echo ALL_DONE
