#!/usr/bin/env bash
set -euo pipefail

# Simple supervisor for src/pipeline.py
# - sources .env if present
# - runs pipeline in a loop, restarts on non-zero exit
# - logs output to logs/supervise_YYYYMMDD_HHMMSS.log

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/supervise_$(date +%Y%m%d_%H%M%S).log"
ENV_FILE="$PROJECT_ROOT/.env"

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' "$ENV_FILE" | xargs) || true
fi

MAX_CONSECUTIVE_ERRORS=12
consecutive=0

echo "Supervisor started at $(date)" | tee -a "$LOG"
echo "Project root: $PROJECT_ROOT" | tee -a "$LOG"

while true; do
  echo "\n===== $(date) running pipeline.py =====" | tee -a "$LOG"
  # Run the pipeline and capture exit code
  /usr/local/bin/python3 "$PROJECT_ROOT/src/pipeline.py" --input "$PROJECT_ROOT/Data/Startups.xlsx" --output-dir "$PROJECT_ROOT/results" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]:-0}

  if [ "$rc" -eq 0 ]; then
    echo "pipeline.py exited with code 0 (finished) at $(date). Supervisor exiting." | tee -a "$LOG"
    exit 0
  fi

  echo "pipeline.py exited with code $rc at $(date)." | tee -a "$LOG"
  consecutive=$((consecutive+1))
  if [ "$consecutive" -ge "$MAX_CONSECUTIVE_ERRORS" ]; then
    echo "Reached $MAX_CONSECUTIVE_ERRORS consecutive failures, supervisor exiting." | tee -a "$LOG"
    exit 1
  fi

  # exponential backoff capped at 5 minutes
  sleep_seconds=$(( 10 * consecutive ))
  if [ "$sleep_seconds" -gt 300 ]; then
    sleep_seconds=300
  fi
  echo "Sleeping for ${sleep_seconds}s before restart..." | tee -a "$LOG"
  sleep "$sleep_seconds"
done
