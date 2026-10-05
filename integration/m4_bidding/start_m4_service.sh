#!/usr/bin/env bash
# M4 bidding 分离服务用户态启动脚本（无 systemd / 无 docker，仿 start_m1_service.sh）。
# 用法：bash start_m4_service.sh [start|stop|status]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/yty-s/venv/bin/python
LOG_DIR="$HERE/data/logs"
PID_FILE="$HERE/data/m4_service.pid"
mkdir -p "$LOG_DIR"

# 主 vLLM（192.168.30.215:8000），与 run_*.sh / m1_service 一致
export LLM_BASE_URL="${LLM_BASE_URL:-http://192.168.30.215:8000/v1}"
export LLM_MODEL="${LLM_MODEL:-Qwen/Qwen3.6-35B-A3B}"
export LLM_API_KEY="${LLM_API_KEY:-EMPTY}"
export LLM_TEMPERATURE="${LLM_TEMPERATURE:-0}"
export LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-65536}"
export LLM_ENABLE_THINKING="${LLM_ENABLE_THINKING:-false}"
# 8090/8091 为 m1_staging/m1_service 设计端口；8092 本机已被其他服务占用，
# 服务器验证通过的默认端口为 8093。
export M4_SERVICE_PORT="${M4_SERVICE_PORT:-8093}"
# 正式形态注入：M4_BIDDING_DSN='mysql://m1_dev:<密码>@192.168.30.216:3306/m1_staging'
# 缺省落 SQLite 自测库 data/m4_bidding.db

cmd="${1:-start}"
case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "已在运行 pid=$(cat "$PID_FILE")"; exit 0
    fi
    cd "$HERE"
    nohup "$PY" -m uvicorn service:app --host 0.0.0.0 --port "$M4_SERVICE_PORT" \
      > "$LOG_DIR/m4_service.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "已启动 pid=$(cat "$PID_FILE") 日志=$LOG_DIR/m4_service.log"
    ;;
  stop)
    [[ -f "$PID_FILE" ]] && kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE" && echo "已停止" || echo "未在运行"
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PID_FILE")"
      curl -s "http://127.0.0.1:$M4_SERVICE_PORT/healthz" || true
    else
      echo "stopped"
    fi
    ;;
  *) echo "用法: $0 [start|stop|status]"; exit 1 ;;
esac
