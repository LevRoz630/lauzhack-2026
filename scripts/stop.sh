#!/usr/bin/env bash
# Stop Breakpoint: hub, camera detector and its watchdog, screen cover, game, any recording.
fuser -k 8770/tcp >/dev/null 2>&1
pkill -f 'bash scripts/watch_camer[a]' 2>/dev/null
pkill -f '[.]venv/bin/python[0-9.]* -m sense' 2>/dev/null
pkill -f 'python[0-9.]* -m hub[.]overlay' 2>/dev/null
pkill -f 'whack_a_mol[e][.]py' 2>/dev/null
pkill -x arecord 2>/dev/null
echo "Stopped."
