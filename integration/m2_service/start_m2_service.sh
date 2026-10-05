#!/usr/bin/env bash
# M2 语义归并服务用户态启动脚本（无 systemd / 无 docker，仿 start_m4_service.sh）。
# 用法：
#   M2_DSN='mysql://m1_dev:<密码>@192.168.30.216:3306/m1_staging' \
#       bash start_m2_service.sh start|stop|status
#
# 不给 M2_DSN 时缺省落本机 SQLite 自测库 data/m2_staging.db（仅供自测，不是正式形态）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/yty-s/venv/bin/python
LOG_DIR="$HERE/data/logs"
PID_FILE="$HERE/data/m2_service.pid"
mkdir -p "$LOG_DIR"

# 主 vLLM（192.168.30.215:8000），与 run_*.sh / m1_service / m4 一致
export LLM_BASE_URL="${LLM_BASE_URL:-http://192.168.30.215:8000/v1}"
export LLM_MODEL="${LLM_MODEL:-qwen3.8-27b}"
export LLM_API_KEY="${LLM_API_KEY:-EMPTY}"
export LLM_TRANSPORT="${LLM_TRANSPORT:-openai}"
export LLM_TEMPERATURE="${LLM_TEMPERATURE:-0}"
# Qwen3.x 思考型模型不关思考 → content 为 null，JSON 解析必然失败
export LLM_ENABLE_THINKING="${LLM_ENABLE_THINKING:-false}"
export LLM_TIMEOUT="${LLM_TIMEOUT:-900}"
# 与 M1 不同：M1 generic 整篇单调用要吐 2w+ token，必须 65536；
# M2 每次只把两条 Item 交给模型，输出很短，8192 绰绰有余（M2 HANDOFF 实测结论）。
export LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-8192}"

# 8090/8091/8092/8095 本机已被其他服务占用；1809x 是本项目集成层已用段
# （18090=m1_staging、18091=m1_service、18092=m1_web），M2 取 18093。
export M2_SERVICE_PORT="${M2_SERVICE_PORT:-18093}"
# 两两判定的 LLM 并发数。实测 10 份会议 / 1355 items / 440 次调用约 125s（workers=8）。
export M2_WORKERS="${M2_WORKERS:-4}"

cmd="${1:-start}"
case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "已在运行 pid=$(cat "$PID_FILE")"; exit 0
    fi
    cd "$HERE"
    nohup "$PY" -m uvicorn service:app --host 0.0.0.0 --port "$M2_SERVICE_PORT" \
      > "$LOG_DIR/m2_service.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "已启动 pid=$(cat "$PID_FILE") 日志=$LOG_DIR/m2_service.log"
    echo "健康检查: curl http://127.0.0.1:$M2_SERVICE_PORT/healthz"
    ;;
  stop)
    [[ -f "$PID_FILE" ]] && kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE" && echo "已停止" || echo "未在运行"
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running pid=$(cat "$PID_FILE")"
      curl -s "http://127.0.0.1:$M2_SERVICE_PORT/healthz" || true
      echo
    else
      echo "stopped"
    fi
    ;;
  *) echo "用法: $0 [start|stop|status]"; exit 1 ;;
esac
