#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export M6_DATA_DIR="${M6_DATA_DIR:-$PWD/data}"
mkdir -p "$M6_DATA_DIR/logs"
PYTHON="${M6_PYTHON:-/home/yty-s/venvs/m6-service/bin/python}"
# Foreground exec: supervisor/nohup owns lifecycle; never guess or kill a PID.
exec "$PYTHON" -m uvicorn platform_app:build_app --factory --host "${M6_SERVICE_HOST:-0.0.0.0}" --port "${M6_SERVICE_PORT:-18096}" --workers 1
