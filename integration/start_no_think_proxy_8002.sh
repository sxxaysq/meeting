#!/usr/bin/env bash
# 禁思考代理（8002 → 192.168.30.215:8000）用户态启动脚本。
# 用法：bash start_no_think_proxy_8002.sh [start|stop|status]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/yty/m1x_venv/bin/python
LOG_DIR="$HERE/data/logs"
PID_FILE="$HERE/data/no_think_proxy.pid"
mkdir -p "$LOG_DIR"

export NO_THINK_BACKEND_URL="${NO_THINK_BACKEND_URL:-http://192.168.30.215:8000}"
export NO_THINK_PROXY_PORT="${NO_THINK_PROXY_PORT:-8002}"

cmd="${1:-start}"
case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "已在运行 pid=$(cat "$PID_FILE")"; exit 0
    fi
    nohup "$PY" "$HERE/no_think_proxy_8002.py" > "$LOG_DIR/no_think_proxy.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "已启动 pid=$(cat "$PID_FILE") 日志=$LOG_DIR/no_think_proxy.log"
    ;;
  stop)
    [[ -f "$PID_FILE" ]] && kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE" && echo "已停止" || echo "未在运行"
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PID_FILE")"
      curl -s "http://127.0.0.1:$NO_THINK_PROXY_PORT/health" || true
    else
      echo "stopped"
    fi
    ;;
  *) echo "用法: $0 [start|stop|status]"; exit 1 ;;
esac
