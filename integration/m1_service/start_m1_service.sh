#!/usr/bin/env bash
# Track B m1-service 用户态启动脚本（无 systemd / 无 docker）。
# 用法：bash start_m1_service.sh [start|stop|status]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/yty-s/venv/bin/python
LOG_DIR="$HERE/data/logs"
PID_FILE="$HERE/data/m1_service.pid"
mkdir -p "$LOG_DIR"

# 主 vLLM（192.168.30.215:8000），模型名以 /v1/models 实测为准：qwen3.8-27b
export LLM_BASE_URL="${LLM_BASE_URL:-http://192.168.30.215:8000/v1}"
export LLM_MODEL="${LLM_MODEL:-qwen3.8-27b}"
export LLM_API_KEY="${LLM_API_KEY:-EMPTY}"
export LLM_TEMPERATURE="${LLM_TEMPERATURE:-0}"
export LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-65536}"   # generic 模式整篇单调用需要大输出上限
# Qwen3.x 必须禁思考；vLLM 支持 chat_template_kwargs 透传
export LLM_ENABLE_THINKING="${LLM_ENABLE_THINKING:-false}"
export M1_SERVICE_PORT="${M1_SERVICE_PORT:-8091}"

cmd="${1:-start}"
case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "已在运行 pid=$(cat "$PID_FILE")"; exit 0
    fi
    cd "$HERE"
    nohup "$PY" -m uvicorn app:app --host 0.0.0.0 --port "$M1_SERVICE_PORT" \
      > "$LOG_DIR/m1_service.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "已启动 pid=$(cat "$PID_FILE") 日志=$LOG_DIR/m1_service.log"
    ;;
  stop)
    [[ -f "$PID_FILE" ]] && kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE" && echo "已停止" || echo "未在运行"
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PID_FILE")"
      curl -s "http://127.0.0.1:$M1_SERVICE_PORT/healthz" || true
    else
      echo "stopped"
    fi
    ;;
  *) echo "用法: $0 [start|stop|status]"; exit 1 ;;
esac
