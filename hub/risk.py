"""Rule-based risk tier for a Claude Code tool call. No model involved."""
import re

GREEN, AMBER, RED = "green", "amber", "red"

SAFE_TOOLS = {
    "Read", "Grep", "Glob", "LS", "WebSearch", "TodoWrite",
    "Task", "Agent", "ToolSearch", "Skill", "AskUserQuestion", "NotebookRead",
}

RED_BASH = [
    r"\brm\b", r"\bsudo\b", r"\bdd\b", r"\bmkfs", r"\bshred\b", r"\bchmod\b", r"\bchown\b",
    r"git\s+push\b.*(--force|-f\b|\s\+)", r"git\s+reset\s+--hard", r"git\s+clean\b", r"git\s+branch\s+-D",
    r"(curl|wget)\b.*\|\s*\w+", r"<\(\s*(curl|wget)", r"\bp?kill(all)?\b",
    r"\bfind\b.*(-delete|-exec)", r"--pre[= ]", r"drop\s+(table|database)", r">\s*/dev/", r"\bshutdown\b|\breboot\b",
    r"\.claude/settings",
]
# files an agent has no business reading or writing without the user at the terminal
SECRET_PATH = re.compile(r"(^|[/\s'\"=])\.env(?!\.example)(\.|[\s'\"]|$)|id_rsa|id_ed25519|\.ssh/|\.aws/|\.gnupg/|credentials|\.netrc|\.claude/settings", re.I)

SAFE_BASH = [
    r"ls", r"cat", r"head", r"tail", r"wc", r"pwd", r"echo", r"grep", r"rg", r"which",
    r"git\s+(status|diff|log|show)", r"pytest", r"npm\s+(test|run\s+test)", r"uv\s+run\s+pytest",
    r"python3?\s+-m\s+pytest", r"date", r"true",
]
_SAFE = re.compile(r"^\s*(" + "|".join(SAFE_BASH) + r")(\s|$)")
_CHAIN = re.compile(r"[;&|`\n\r<]|\$\(|>")       # anything that could run a second command or redirect


def _paths(tool_input: dict) -> str:
    return " ".join(str(tool_input.get(k, "")) for k in ("file_path", "path", "notebook_path", "pattern", "url", "command"))


def tier(tool_name: str, tool_input: dict) -> str:
    if SECRET_PATH.search(_paths(tool_input)):
        return RED
    if tool_name == "WebFetch":
        return AMBER          # a URL can carry data out
    if tool_name in SAFE_TOOLS:
        return GREEN
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        if any(re.search(p, cmd, re.I) for p in RED_BASH):
            return RED
        if _SAFE.match(cmd) and not _CHAIN.search(cmd):
            return GREEN
        return AMBER
    return AMBER


def describe(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        return str(tool_input.get("command", ""))[:200]
    for key in ("file_path", "notebook_path", "url", "pattern", "query"):
        if key in tool_input:
            return f"{tool_name} {tool_input[key]}"[:200]
    return tool_name
