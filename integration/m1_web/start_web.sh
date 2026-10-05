#!/usr/bin/env bash
# M1 可视化面板启动脚本
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/yty-s/venv/bin/python
LOG_DIR="$HERE/data/logs"
PID_FILE="$HERE/data/m1_web.pid"
mkdir -p "$LOG_DIR"

export M1_WEB_PORT="${M1_WEB_PORT:-18092}"

cmd="${1:-start}"
case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "已在运行 pid=$(cat "$PID_FILE")"; exit 0
    fi
    cd "$HERE"
    nohup "$PY" -m uvicorn app:app --host 0.0.0.0 --port "$M1_WEB_PORT" \
      > "$LOG_DIR/m1_web.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "已启动 pid=$(cat "$PID_FILE")"
    echo "访问地址: http://127.0.0.1:$M1_WEB_PORT/"
    echo "局域网:   http://192.168.30.216:$M1_WEB_PORT/"
    ;;
  stop)
    [[ -f "$PID_FILE" ]] && kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE" && echo "已停止" || echo "未在运行"
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PID_FILE")"
    else
      echo "stopped"
    fi
    ;;
  *) echo "用法: $0 [start|stop|status]"; exit 1 ;;
esac
