#!/usr/bin/env bash
# Create three small demo projects wired to the hub and open a Claude Code session in each.
# Usage: bash scripts/demo_sessions.sh        (the hub must be running)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEMO="${DEMO_DIR:-$HOME/lauzhack-demo}"
HUB="http://127.0.0.1:8770"

make_project() { # name
  local dir="$DEMO/$1"
  mkdir -p "$dir"
  python3 "$REPO/scripts/install_hooks.py" "$dir" >/dev/null
  [ -d "$dir/.git" ] || git -C "$dir" init -q
}

make_project api
[ -f "$DEMO/api/app.py" ] || printf 'def add(a, b):\n    return a + b\n\n\nif __name__ == "__main__":\n    print(add(2, 3))\n' > "$DEMO/api/app.py"
make_project web
[ -f "$DEMO/web/index.html" ] || printf '<!doctype html>\n<title>Old title</title>\n<h1>Hello</h1>\n' > "$DEMO/web/index.html"
make_project docs
[ -f "$DEMO/docs/notes.txt" ] || printf 'The hub holds risky agent calls until the user is free.\n' > "$DEMO/docs/notes.txt"

launch() { # name prompt
  local dir="$DEMO/$1" before after wid
  before="$(wmctrl -l | cut -d' ' -f1)"
  gnome-terminal --window --title="agent: $1" --working-directory="$dir" -- claude "$2"
  for _ in $(seq 1 20); do
    sleep 0.3
    after="$(wmctrl -l | cut -d' ' -f1)"
    wid="$(comm -13 <(echo "$before" | sort) <(echo "$after" | sort) | head -1)"
    [ -n "$wid" ] && break
  done
  [ -n "${wid:-}" ] && curl -s -X POST "$HUB/window" -d "{\"cwd\":\"$dir\",\"wid\":\"$wid\"}" >/dev/null
  echo "$1: window ${wid:-not found}"
}

launch api  "Read app.py, add a subtract function next to add, then run: python3 app.py"
launch web  "Install the left-pad package with npm install, then use it in a new file pad.js that pads the string 7 to width 3."
launch docs "List the files here, read notes.txt, then write a README.md that summarises it in three lines."
echo "Accept the trust prompt in each new terminal the first time."
