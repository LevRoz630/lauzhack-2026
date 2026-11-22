"""Short labels for the keypad, from a small model working on text only.

A key is 118 pixels wide, so an agent is named by two words taken from the task the user gave it,
and a held call is described in a few plain words. One short request each, cached; any failure
falls back to the folder name or the raw command.
"""
import os
import re
from pathlib import Path

import anthropic

MODEL = "claude-haiku-5-5"
_client: anthropic.AsyncAnthropic | None = None
_cache: dict[tuple[str, str], str] = {}
tokens = {"in": 0, "out": 0}

TITLE = ("You name a coding task for a tiny screen. Reply with only a label of at most two short words, "
         "lowercase, no punctuation, that tells this task apart from others.")
WANT = ("A coding agent wants to run the tool call below. Say what it wants in at most five plain words, "
        "lowercase, no punctuation, starting with a verb. Reply with only those words.")
OUTCOME = ("A coding agent has just finished a task. Given the task and its final message, say in one plain "
           "complete sentence of at most 18 words what it did, what the result was, and anything the user "
           "must do next. Do not start with \"the agent\". No preamble. Reply with only that sentence.")


def load_env() -> None:
    """Read ANTHROPIC_API_KEY from the repo's .env if it is not already set."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if "ANTHROPIC_API_KEY" in os.environ or not env.exists():
        return
    for line in env.read_text().splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip("'\"")


async def _ask(system: str, text: str, max_words: int, keep_sentence: bool = False) -> str:
    global _client
    key = (system, text)
    if key in _cache:
        return _cache[key]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    if _client is None:
        _client = anthropic.AsyncAnthropic(timeout=8.0, max_retries=1)
    try:
        r = await _client.messages.create(model=MODEL, max_tokens=1000, output_config={"effort": "low"},
                                          system=system, messages=[{"role": "user", "content": text[:1500]}])
    except (anthropic.APIStatusError, anthropic.APIConnectionError):
        return ""
    tokens["in"] += r.usage.input_tokens
    tokens["out"] += r.usage.output_tokens
    if r.stop_reason != "end_turn":
        return ""
    out = " ".join(b.text for b in r.content if b.type == "text")
    if keep_sentence:      # a summary keeps its capitals and punctuation
        words = out.split()
        _cache[key] = " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")
    else:                  # a key label is bare lowercase words
        _cache[key] = " ".join(re.sub(r"[^\w\s.-]", "", out).lower().split()[:max_words])
    return _cache[key]


async def title(prompt: str) -> str:
    return await _ask(TITLE, prompt, 2)


async def want(call: str) -> str:
    return await _ask(WANT, call, 5)


async def outcome(task: str, final: str) -> str:
    return await _ask(OUTCOME, f"Task: {task[:400]}\n\nFinal message: {final[:1500]}", 30, keep_sentence=True)
