#!/usr/bin/env python3
"""Point a project's Claude Code sessions at the hub.

Usage: python3 scripts/install_hooks.py /path/to/project
       python3 scripts/install_hooks.py --observe /path/to/project   (shown on the keypad, never held)
Writes the hooks into <project>/.claude/settings.local.json (kept out of git by Claude Code).
"""
import json
import sys
from pathlib import Path

URL = "http://127.0.0.1:8770/hook"
EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "StopFailure", "Notification"]


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--observe"]
    if len(args) != 1:
        sys.exit(__doc__)
    global URL
    if "--observe" in sys.argv:      # show the session on the keypad without ever holding its calls
        URL += "?observe=1"
    path = Path(args[0]).expanduser() / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    for ev in EVENTS:
        hooks[ev] = [{"hooks": [{"type": "http", "url": URL, "timeout": 5}]}]
    hooks["PreToolUse"] = [{"hooks": [{"type": "http", "url": URL, "timeout": 600}]}]
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"hooks written to {path}")


if __name__ == "__main__":
    main()
