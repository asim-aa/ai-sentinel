#!/usr/bin/env bash
# Starts the demo AI service (:8000) and the AI Sentinel dashboard (:8500) together.
set -euo pipefail
cd "$(dirname "$0")/.."

export SENTINEL_DB_PATH="${SENTINEL_DB_PATH:-$(pwd)/sentinel.db}"
rm -f "$SENTINEL_DB_PATH" "$SENTINEL_DB_PATH-wal" "$SENTINEL_DB_PATH-shm"

echo "ai-sentinel: starting demo AI service on :8000"
uv run uvicorn demo_service.main:app --port 8000 --log-level warning &
DEMO_PID=$!

echo "ai-sentinel: starting dashboard + engine on :8500"
uv run uvicorn ai_sentinel.dashboard.server:app --port 8500 --log-level warning &
SENTINEL_PID=$!

cleanup() {
  echo "ai-sentinel: stopping..."
  kill "$DEMO_PID" "$SENTINEL_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait
