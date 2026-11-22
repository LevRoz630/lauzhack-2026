#!/usr/bin/env bash
# Start Breakpoint: hub, camera detector with its watchdog, screen cover. Logs go to logs/.
# Usage: bash scripts/start.sh            cover the screen while away, never lock
#        bash scripts/start.sh --lock     also lock the laptop after 5 s away
#        bash scripts/start.sh --keep     keep the agents already on the keys (a restart mid-session)
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
bash scripts/stop.sh >/dev/null 2>&1
[ "${1:-}" = "--keep" ] || [ "${2:-}" = "--keep" ] || rm -f .hub_state.json      # default: no agents, counter at zero
(setsid uv run python -m hub.hub > logs/hub.log 2>&1 &)
for _ in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:8770/state && break; sleep 0.5; done
(setsid uv run python -m sense.sense > logs/sense.log 2>&1 &)
(setsid bash scripts/watch_camera.sh >/dev/null 2>&1 &)
if [ "${1:-}" = "--lock" ] || [ "${2:-}" = "--lock" ]; then
  (setsid /usr/bin/python3 -m hub.overlay > logs/overlay.log 2>&1 &)      # the system Python has proper fonts
else
  (setsid /usr/bin/python3 -m hub.overlay --no-lock > logs/overlay.log 2>&1 &)
fi
echo "Started. Keypad page: http://localhost:8770/keypad (open it in Chrome yourself, in one tab only)."
echo "Agents: bash scripts/demo_sessions.sh"
