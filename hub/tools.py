"""What the launcher keys open. Only the apps named here can be started from the keypad."""
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

HOOK_URL = "http://127.0.0.1:8770/hook"
HOOK_EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "StopFailure", "Notification"]
AGENT_POOL = Path.home() / "lauzhack-demo" / "agents"   # new agents start in new-1 .. new-4; this folder is trusted once
APPS = ("notes", "claude", "search", "agent")


def _run(argv: list[str]) -> None:
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def install_hooks(project: Path) -> None:
    """Point a project's Claude Code sessions at the hub (same as scripts/install_hooks.py)."""
    path = project / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    for ev in HOOK_EVENTS:
        hooks[ev] = [{"hooks": [{"type": "http", "url": HOOK_URL, "timeout": 5}]}]
    hooks["PreToolUse"] = [{"hooks": [{"type": "http", "url": HOOK_URL, "timeout": 600}]}]
    path.write_text(json.dumps(data, indent=2) + "\n")


def new_agent(busy: set[str], windows: dict[str, str]) -> str:
    """Open a new Claude Code session in a free pool folder and return that folder."""
    folder = next((AGENT_POOL / f"new-{i}" for i in range(1, 5) if str(AGENT_POOL / f"new-{i}") not in busy),
                  AGENT_POOL / "new-1")
    folder.mkdir(parents=True, exist_ok=True)
    install_hooks(folder)

    def ids() -> set[str]:      # window ids only: titles change while other sessions animate theirs
        return {line.split()[0] for line in subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout.splitlines() if line}

    before = ids()
    _run(["gnome-terminal", "--window", f"--title=agent: {folder.name}", f"--working-directory={folder}", "--", "claude"])
    for _ in range(20):      # find the new terminal window so its key can bring it forward
        time.sleep(0.3)
        fresh = ids() - before
        if fresh:
            windows[str(folder)] = fresh.pop()
            break
    return str(folder)


def open_tool(app: str, text: str, busy: set[str], windows: dict[str, str]) -> bool | str:
    """Open an app; for search, text is the query."""
    if app not in APPS:
        return False
    if app == "search":
        _run(["xdg-open", "https://www.google.com/search?q=" + quote(text) if text else "https://www.google.com/"])
    elif app == "claude":
        _run(["xdg-open", "https://claude.ai/new"])
    elif app == "notes":
        _run(["flatpak", "run", "md.obsidian.Obsidian"])
    elif app == "agent":
        return new_agent(busy, windows)
    return True
