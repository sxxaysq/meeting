#!/usr/bin/env bash
# m1_staging ingest 服务用户态启动脚本（无 systemd / 无 docker）。
# 用法：bash start_staging.sh [start|stop|status]
#
# DB 方言由 M1_STAGING_DSN 决定：
#   未设置        → SQLite 自测库 data/m1_staging.db
#   mysql://u:p@host:port/dbname → MySQL 8（正式形态，DDL 见 schema.sql）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/yty/m1x_venv/bin/python
LOG_DIR="$HERE/data/logs"
PID_FILE="$HERE/data/m1_staging.pid"
mkdir -p "$LOG_DIR"

export M1_STAGING_PORT="${M1_STAGING_PORT:-8090}"

cmd="${1:-start}"
case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "已在运行 pid=$(cat "$PID_FILE")"; exit 0
    fi
    cd "$HERE"
    nohup "$PY" -m uvicorn service:app --host 0.0.0.0 --port "$M1_STAGING_PORT" \
      > "$LOG_DIR/m1_staging.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "已启动 pid=$(cat "$PID_FILE") 日志=$LOG_DIR/m1_staging.log"
    ;;
  stop)
    [[ -f "$PID_FILE" ]] && kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE" && echo "已停止" || echo "未在运行"
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PID_FILE")"
      curl -s "http://127.0.0.1:$M1_STAGING_PORT/healthz" || true
    else
      echo "stopped"
    fi
    ;;
  *) echo "用法: $0 [start|stop|status]"; exit 1 ;;
esac
