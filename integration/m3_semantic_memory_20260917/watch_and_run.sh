#!/bin/bash
# Wait for the qwen LLM (192.168.30.215:8000) to recover, then auto-launch the
# full 15-meeting semantic re-run. Bounded to ~4h. Logs to watcher.log.
# The run itself is the same native_full_dataset invocation, output to full_semantic_v2.
WORK=/home/yty-s/meeting-m2-work/integration/m3_semantic_memory_20260917
CAND=$WORK/candidate
FROZEN=/home/yty-s/meeting-m2-work/integration/m1_m3_m6_20260915/frozen_m1
PY=/home/yty-s/venvs/m6-service/bin/python
OUT=$WORK/runs/full_semantic_v2
LOG=$OUT/watcher.log
mkdir -p "$OUT"
echo "[watcher] started $(date -Is); polling 192.168.30.215:8000 every 60s (max 240 tries)" >> "$LOG"
for i in $(seq 1 240); do
  body=$(curl -s --max-time 6 http://192.168.30.215:8000/v1/models 2>/dev/null)
  if echo "$body" | grep -qi "qwen"; then
    echo "[watcher] LLM recovered at $(date -Is): $body" >> "$LOG"
    # fresh output dir for the relaunched run
    rm -f "$OUT/failure.json"
    cd "$CAND" && nohup "$PY" integration/m6_service/native_full_dataset.py \
      --inputs "$FROZEN" --departments "$WORK/departments.json" --output "$OUT" \
      >> "$OUT/run.log" 2>&1 &
    echo "[watcher] LAUNCHED full re-run pid=$! at $(date -Is)" >> "$LOG"
    exit 0
  fi
  sleep 60
done
echo "[watcher] LLM did not recover within ~4h; giving up at $(date -Is)" >> "$LOG"
exit 1
