#!/usr/bin/env bash
# Restart the camera detector whenever the hub reports that the camera has stopped.
# A stalled camera stream does not recover inside the process, but a fresh process does.
set -uo pipefail
cd "$(dirname "$0")/.."
while sleep 4; do
  if curl -s -m 2 http://127.0.0.1:8770/state | grep -q '"reason": "camera not running"'; then
    pkill -f '[.]venv/bin/python[0-9.]* -m sense'
    sleep 1.5
    (setsid uv run python -m sense.sense > logs/sense.log 2>&1 &)
    sleep 12
  fi
done
