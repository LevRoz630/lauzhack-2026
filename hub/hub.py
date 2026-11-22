"""Hub: receives Claude Code hook events, holds risky tool calls, answers them later.

Run:  uv run python -m hub.hub
Page: http://127.0.0.1:8770/
"""
import asyncio
import itertools
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import names, risk, tools
from .voice import Voice

PORT = 8770
HOLD_LIMIT = 570  # seconds; the hook's own timeout is 600
PAGE = Path(__file__).with_name("page.html")
KEYPAD = Path(__file__).with_name("keypad.html")
DIAGNOSTICS = Path(__file__).with_name("diagnostics.html")
GAME = Path(__file__).resolve().parent.parent / "game"
VENDOR_JS = Path(__file__).resolve().parent.parent / "vendor" / "mx-creative-console-webhid" / "js"

sessions: dict[str, dict] = {}
pending: dict[str, list] = {}
clients: set[web.WebSocketResponse] = set()
windows: dict[str, str] = {}
_ids = itertools.count(1)
voice = Voice()
lips: list = []

user = {"state": "unknown", "reason": "camera not running", "features": {}, "t": 0.0, "desk": {}}
manual_until = 0.0
shoulder_off_until = 0.0
SHOULDER_OFF_SECONDS = 300
MANUAL_SECONDS = 30
SENSE_TIMEOUT = 8.0      # longer than a page photo, during which sense is silent
_last_effective = "unknown"
_state_since = time.time()
IDLE_TIMER = 30.0        # what a plain idle timer would use to decide you have left
idle_misfires = 0        # times that timer would have released while the camera saw you focused
_idle_armed = True
STATE_FILE = Path(__file__).resolve().parent.parent / ".hub_state.json"
TIMELINE = Path(__file__).resolve().parent.parent / "logs" / "timeline.jsonl"
_last_kind = ""


def kind_of(state: str, reason: str) -> str:
    """One word for the timeline: typing, reading, thinking, distracted, free, shoulder, away, off."""
    if state == "focused":
        if "thinking" in reason:
            return "thinking"
        return "typing" if reason.startswith("typing") else "reading"
    return {"available": "free", "unknown": "off"}.get(state, state)


def log_kind() -> None:
    """Append a line whenever the kind of activity changes."""
    global _last_kind
    st, reason = effective()
    kind = kind_of(st, reason)
    if kind != _last_kind:
        _last_kind = kind
        TIMELINE.parent.mkdir(exist_ok=True)
        with TIMELINE.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "kind": kind}) + "\n")


async def timeline(req: web.Request) -> web.Response:
    """Segments and totals for the last N minutes, for the live page."""
    minutes = float(req.query.get("minutes", "60"))
    now = time.time()
    start = now - minutes * 60
    rows = []
    if TIMELINE.exists():
        for line in TIMELINE.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    segs = []
    for i, r in enumerate(rows):
        t0, t1 = r["t"], rows[i + 1]["t"] if i + 1 < len(rows) else now
        if t1 > start and r["kind"] != "off":
            segs.append({"t0": max(t0, start), "t1": t1, "kind": r["kind"]})
    totals: dict[str, float] = {}
    for sg in segs:
        totals[sg["kind"]] = totals.get(sg["kind"], 0) + (sg["t1"] - sg["t0"]) / 60
    focus = ("typing", "reading", "thinking")
    streak = best = 0.0
    for sg in segs:                         # longest unbroken focus, ignoring breaks under 10 seconds
        if sg["kind"] in focus or sg["t1"] - sg["t0"] < 10:
            streak += sg["t1"] - sg["t0"]
            best = max(best, streak)
        else:
            streak = 0.0
    return web.json_response({"start": start, "now": now, "segments": segs,
                              "minutes": {k: round(v, 1) for k, v in totals.items()},
                              "longest_focus_min": round(best / 60, 1),
                              "distractions": sum(1 for sg in segs if sg["kind"] == "distracted")})


last_press = 0.0         # when a keypad key was last pressed
PRESS_PRESENT = 8.0      # seconds a keypad press counts as "I am here"


def effective() -> tuple[str, str]:
    """The state the hub acts on. Without a camera feed nothing is hidden."""
    now = time.time()
    if now < manual_until:
        return "available", "manual"
    if now - user["t"] > SENSE_TIMEOUT:
        return "unknown", "camera not running"
    if user["state"] == "shoulder" and now < shoulder_off_until:
        return "focused", "shoulder switched off"
    # Looking down at the keypad, or holding a page up to the camera, hides the face. Neither is leaving.
    if user["state"] == "away" and (now - last_press < PRESS_PRESENT or now < shot.get("live_until", 0)):
        return "focused", "at the keypad"
    return user["state"], user["reason"]


def status_of(s: dict, held: list) -> str:
    """The one word shown for an agent.

    needs you: a risky call is held for your decision, or the agent asked you a question.
    working:   its turn is in progress and nothing is held (thinking, reading, running safe tools).
    done:      it ended its turn and is idle until you give it a new instruction.
    failed:    its turn ended with an error.
    """
    if held or s.get("state") == "asking":
        return "needs you"
    if s.get("state") in ("done", "failed"):
        return s["state"]
    return "working"


NUDGE_AFTER = float(os.environ.get("NUDGE_AFTER", "10"))
WELCOME_SECONDS = 5.0
SUMMARY_SECONDS = 15.0
MIN_ABSENCE = 6.0        # seconds away or on the phone before returning earns a greeting; same as sense's MIN_BREAK
NUDGE = "Whenever you are ready, your desk is ready too."
WELCOME = "Good to have you back."
_welcome = {"until": 0.0, "text": ""}


def nudge_text(st: str, waiting: int) -> str:
    """A kind line while the user is on the phone, and a short welcome when they return."""
    now = time.time()
    if now < _welcome["until"]:
        return _welcome["text"]
    if st != "distracted" or now - _state_since < NUDGE_AFTER:
        return ""
    line = NUDGE
    if waiting:
        line += f" {waiting} agent{'s' if waiting != 1 else ''} would love a word when you are back."
    return line


def overview() -> None:
    """The summary key: one line for every agent, whatever its state, so you know which one to open."""
    lines = []
    for sid, s in sessions.items():
        held = pending.get(sid, [])
        name = s.get("title") or s["name"]
        status = status_of(s, held)
        if held:
            lines.append(f"{name}: needs you. Wants to {held[0]['want'] or held[0]['ask'][:40]}")
        elif status == "needs you":
            lines.append(f"{name}: needs you. It asked a question")
        elif status == "working":
            lines.append(f"{name}: still working" + (f", last step {s['doing']}" if s.get("doing") else ""))
        else:
            lines.append(f"{name}: {status}. {s.get('outcome') or ''}".rstrip(". ") + ".")
    if lines:
        _welcome.update(until=time.time() + SUMMARY_SECONDS, text="\n".join(["Your agents"] + lines[:6]))


def welcome(waiting: int) -> None:
    """Called when the user comes back from a real absence: a short greeting. The summary of what the
    agents did is shown only when asked for, with the summary key."""
    text = WELCOME
    if waiting:
        text += f" {waiting} agent{'s have' if waiting != 1 else ' has'} something for you."
    _welcome.update(until=time.time() + WELCOME_SECONDS, text=text)


def snapshot() -> dict:
    out = []
    for sid, s in sessions.items():
        held = pending.get(sid, [])
        first = held[0] if held else None
        out.append({
            **s,
            "status": status_of(s, held),
            "held": len(held),
            "held_id": first["id"] if first else None,
            "want": (first["want"] or first["ask"]) if first else None,
            "tier": first["tier"] if first else None,
        })
    st, reason = effective()
    waiting = sum(1 for o in out if o["status"] == "needs you")
    return {"type": "sessions", "sessions": out, "page_v": KEYPAD.stat().st_mtime,
            "user": {"state": st, "reason": reason, "features": user["features"],
                     "for_s": round(time.time() - _state_since, 1), "nudge": nudge_text(st, waiting),
                     "idle_misfires": idle_misfires, "model_tokens": names.tokens["in"] + names.tokens["out"],
                     "desk": user["desk"],
                     "front": front, "page_t": shot["t"], "game": game_running(),
                     "page_preview": "live" if time.time() < shot.get("live_until", 0) else ("shot" if time.time() - shot["t"] < 6 else ""),
                     "manual_left": max(0, round(manual_until - time.time())),
                     "shoulder_off_left": max(0, round(shoulder_off_until - time.time()))}}


async def send_all(text: str, skip: web.WebSocketResponse | None = None) -> None:
    for ws in list(clients):
        if ws is skip:
            continue
        try:
            await ws.send_str(text)
        except Exception:
            clients.discard(ws)


async def broadcast() -> None:
    await send_all(json.dumps(snapshot()))


def session_for(d: dict) -> dict:
    sid = d.get("session_id", "?")
    if sid not in sessions:
        cwd = d.get("cwd") or ""
        sessions.pop("new:" + cwd.rstrip("/"), None)
        sessions[sid] = {"id": sid, "name": os.path.basename(cwd.rstrip("/")) or sid[:6],
                         "cwd": cwd, "state": "running"}
    return sessions[sid]


async def name_session(s: dict, prompt: str) -> None:
    label = await names.title(prompt)
    if label:
        s["title"] = label
        await broadcast()


async def summarise(s: dict, final: str) -> None:
    s["outcome"] = await names.outcome(s.get("task", ""), final) or "finished"


async def describe_call(item: dict) -> None:
    item["want"] = await names.want(item["ask"])
    await broadcast()


def decision(behavior: str | None, said: str = "") -> dict:
    if behavior is None:
        return {}
    reason = {"allow": "Approved from the keypad", "deny": "Denied from the keypad",
              "ask": "Needs approval in the terminal"}[behavior]
    if behavior == "deny" and said:
        reason = f"The user denied this and said: {said}"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": behavior,
                                   "permissionDecisionReason": reason}}


async def hook(req: web.Request) -> web.Response:
    d = await req.json()
    ev = d.get("hook_event_name")
    sid = d.get("session_id", "?")
    s = session_for(d)

    if ev == "SessionEnd":
        sessions.pop(sid, None)
        await broadcast()
        return web.json_response({})

    if ev == "PreToolUse":
        name, inp = d.get("tool_name", ""), d.get("tool_input") or {}
        t = risk.tier(name, inp)
        ask = risk.describe(name, inp)
        s["doing"] = " ".join(os.path.basename(w) if w.startswith("/") else w for w in ask.split())[:60]      # file names, not full paths
        if t == risk.GREEN or req.query.get("observe"):      # an observed session is shown on the keys but never held
            s["state"] = "asking" if name == "AskUserQuestion" else "running"
            await broadcast()
            return web.json_response({})
        item = {"id": next(_ids), "tier": t, "ask": ask, "want": "",
                "fut": asyncio.get_running_loop().create_future()}
        pending.setdefault(sid, []).append(item)
        s["state"] = "waiting"
        await broadcast()
        asyncio.create_task(describe_call(item))
        behavior = None
        try:
            behavior = await asyncio.wait_for(item["fut"], HOLD_LIMIT)
        except asyncio.TimeoutError:
            behavior = "ask" if t == risk.RED else None   # a red call is never let through unanswered
        finally:
            if item in pending.get(sid, []):
                pending[sid].remove(item)
            if sid in sessions and not pending.get(sid):
                s["state"] = "running"
            await broadcast()
        return web.json_response(decision(behavior, item.get("said", "")))

    if ev == "UserPromptSubmit" and d.get("prompt"):
        s["task"] = d["prompt"]
        asyncio.create_task(name_session(s, d["prompt"]))
    if ev in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "SessionStart"):
        if not pending.get(sid):
            s["state"] = "running" if ev != "SessionStart" else "done"
    elif ev == "Stop":
        s["state"] = "done"
        asyncio.create_task(summarise(s, d.get("last_assistant_message") or ""))
    elif ev == "Notification":
        if d.get("notification_type") in ("permission_prompt", "elicitation_dialog"):
            s["state"] = "asking"
    elif ev == "StopFailure":
        s["state"] = "failed"
        s["outcome"] = "failed"
    await broadcast()
    return web.json_response({})


def resolve(sid: str, behavior: str, said: str = "", item_id: int | None = None) -> dict:
    """Answer one held call. With item_id, only that exact call: never a later one the user has not seen."""
    held = pending.get(sid, [])
    if not held or behavior not in ("allow", "deny"):
        return {"ok": False}
    item = held[0] if item_id is None else next((i for i in held if i["id"] == item_id), None)
    if item is None:
        return {"ok": False}
    item["said"] = said
    if behavior == "allow" and item["tier"] == risk.RED:
        behavior = "ask"
    if not item["fut"].done():
        item["fut"].set_result(behavior)
    if behavior == "ask":
        focus(sid)
    return {"ok": True, "sent": behavior}


def type_into_terminal(sid: str, text: str) -> bool:
    """Type the spoken text into the session's terminal and press Enter."""
    sess = sessions.get(sid) or {}
    wid = windows.get(sess.get("cwd", "").rstrip("/"))
    # Never type while the agent is waiting on a prompt: Enter there would answer the prompt.
    if not wid or not text or status_of(sess, pending.get(sid, [])) == "needs you":
        return False
    if subprocess.run(["xdotool", "windowactivate", "--sync", wid], timeout=3, check=False).returncode != 0:
        return False      # the terminal is gone; do not type into whatever else is in front
    subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "6", "--", text], timeout=20, check=False)
    subprocess.run(["xdotool", "key", "--clearmodifiers", "Return"], timeout=3, check=False)
    return True


last_app, last_launch = "", 0.0
shot = {"path": "", "t": 0.0}            # the last page photo taken with the page key
game = {"proc": None}                    # the break game, while it runs
front = {"app": "home", "sid": None}


def game_running() -> bool:
    return game["proc"] is not None and game["proc"].poll() is None


# the only keys the keypad may send to the window in front
APP_KEYS = {
    "new_tab": "ctrl+t", "back": "alt+Left", "new_note": "ctrl+n", "enter": "Return", "search_notes": "ctrl+shift+f",
    "interrupt": "Escape",
}


recent_windows: list[int] = []      # windows the user had in front, oldest first
_last_cycle = 0.0


def cycle_window(step: int) -> None:
    """The arrow buttons: go to the next or previous open window, like stepping through tabs."""
    global _last_cycle
    if time.time() - _last_cycle < 0.25:      # one physical press can arrive twice
        return
    _last_cycle = time.time()
    out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=2).stdout
    wins = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        title = parts[3] if len(parts) > 3 else ""
        if len(parts) < 3 or parts[1] == "-1" or title in ("Desktop",) or title.startswith("Keypad - "):
            continue          # skip the desktop itself and the page that drives the keypad
        wins.append(int(parts[0], 16))
    if not wins:
        return
    try:
        current = int(subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=1).stdout.strip())
        i = wins.index(current)
    except (ValueError, subprocess.SubprocessError):
        i = -1 if step > 0 else 0
    subprocess.run(["wmctrl", "-i", "-a", hex(wins[(i + step) % len(wins)])], timeout=2, check=False)


def go_home() -> None:
    """The home key on an app screen: put that app away and go back to the window used before it."""
    if front["app"] == "home" or not recent_windows:
        return
    current = recent_windows[-1]
    subprocess.run(["xdotool", "windowminimize", str(current)], timeout=2, check=False)
    for wid in reversed(recent_windows[:-1]):
        if wid != current and subprocess.run(["xdotool", "windowactivate", str(wid)], timeout=2, check=False,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break


def detect_front() -> dict:
    """Classify the window in front: claude, search, notes, agent (with its session) or home."""
    try:
        wid = int(subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=1).stdout.strip())
        if not recent_windows or recent_windows[-1] != wid:      # remember where the user has been, newest last
            recent_windows.append(wid)
            del recent_windows[:-6]
        props = subprocess.run(["xprop", "-id", str(wid), "WM_CLASS", "_NET_WM_NAME"],
                               capture_output=True, text=True, timeout=1).stdout.lower()
    except Exception:
        return {"app": "home", "sid": None}
    for cwd, w in windows.items():
        try:
            if int(w, 16) == wid:
                sid = next((i for i, s in sessions.items() if s.get("cwd", "").rstrip("/") == cwd), None)
                if sid:
                    return {"app": "agent", "sid": sid}
        except ValueError:
            continue
    title = props.split("_net_wm_name", 1)[-1]
    if "gnome-terminal" in props and any(mark in title for mark in ("✳", "◐", "◑", "◒", "◓", "claude")):
        return {"app": "agent", "sid": None}
    if "obsidian" in props:
        return {"app": "notes", "sid": None}
    if "firefox" in props or "google-chrome" in props:
        if any(own in title for own in ("keypad", "live state", "diagnostics", "camera - ")):      # our own pages
            return {"app": "home", "sid": None}
        return {"app": "claude" if "claude" in title else "search", "sid": None}
    return {"app": "home", "sid": None}


def type_into_active(text: str) -> bool:
    """Type the spoken text at the cursor of the window in front. Enter follows in chats and terminals."""
    if not text:
        return False
    sid = front.get("sid")
    if front["app"] == "agent" and sid in sessions and status_of(sessions[sid], pending.get(sid, [])) == "needs you":
        return False      # that agent is on a prompt; typing and Enter would answer it
    subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "6", "--", text], timeout=20, check=False)
    if front["app"] in ("claude", "agent"):
        subprocess.run(["xdotool", "key", "--clearmodifiers", "Return"], timeout=3, check=False)
    return True


async def launch(app: str) -> None:
    """Open a tool. A new agent gets a key at once, before its session has said anything."""
    folder = await asyncio.get_running_loop().run_in_executor(None, tools.open_tool, app, "", busy_folders(), windows)
    if app == "agent" and isinstance(folder, str):
        sessions["new:" + folder] = {"id": "new:" + folder, "name": os.path.basename(folder), "cwd": folder, "state": "done"}
        await broadcast()


def busy_folders() -> set[str]:
    return {s.get("cwd", "").rstrip("/") for s in sessions.values()}


async def push_to_talk(d: dict) -> None:
    phase, sid, purpose = d.get("phase"), d.get("id", ""), d.get("purpose", "")
    if phase == "start":
        voice.start()
    elif phase == "cancel":
        voice.cancel()
    elif phase == "stop":
        loop = asyncio.get_running_loop()
        text, note = await loop.run_in_executor(None, voice.stop, list(lips))
        if purpose == "deny":
            resolve(sid, "deny", text, d.get("item"))
        elif purpose == "tell":
            await loop.run_in_executor(None, type_into_terminal, sid, text)
        elif purpose == "type":
            if front["app"] == "search" and text:
                await loop.run_in_executor(None, tools.open_tool, "search", text, busy_folders(), windows)
            else:
                await loop.run_in_executor(None, type_into_active, text)
        await send_all(json.dumps({"type": "voice", "text": text, "note": note}))


def focus(sid: str) -> None:
    """Bring the session's terminal window to the front, if the launcher registered it."""
    wid = windows.get((sessions.get(sid) or {}).get("cwd", "").rstrip("/"))
    if wid:
        subprocess.Popen(["wmctrl", "-i", "-a", wid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def window(req: web.Request) -> web.Response:
    d = await req.json()
    windows[str(d.get("cwd", "")).rstrip("/")] = str(d.get("wid", ""))
    return web.json_response({"ok": True})


async def on_sense(d: dict) -> None:
    global _last_effective, _state_since, idle_misfires, _idle_armed
    lips.extend(d.get("lips") or [])
    del lips[:-600]
    user.update(state=d.get("state", "unknown"), reason=d.get("reason", ""),
                features=d.get("features") or {}, t=time.time(), desk=d.get("desk") or {})
    now_state = effective()[0]
    if now_state != _last_effective:
        # Greet only after a real absence. Looking down at the keypad loses the face for a few seconds,
        # which is not "coming back".
        if (_last_effective in ("distracted", "away") and now_state in ("focused", "available")
                and time.time() - _state_since >= MIN_ABSENCE):
            welcome(sum(1 for sid, s in sessions.items() if status_of(s, pending.get(sid, [])) == "needs you"))
        _last_effective, _state_since = now_state, time.time()
    idle = (user["features"] or {}).get("idle_s") or 0.0
    if idle < IDLE_TIMER:
        _idle_armed = True
    elif _idle_armed and now_state == "focused":
        _idle_armed = False
        idle_misfires += 1
    await broadcast()


async def state(_req: web.Request) -> web.Response:
    return web.json_response(snapshot())


async def page(_req: web.Request) -> web.Response:
    return web.Response(text=PAGE.read_text(), content_type="text/html", headers={"Cache-Control": "no-store"})


CAMERA_PAGE = """<!doctype html><meta charset="utf-8"><title>Camera</title>
<body style="margin:0;background:#0d1117;display:flex;justify-content:center">
<img id="v" style="height:100vh;image-rendering:auto">
<script>setInterval(() => { document.getElementById('v').src = '/camera.jpg?' + Date.now(); }, 200);</script>"""


async def camera(_req: web.Request) -> web.Response:
    return web.Response(text=CAMERA_PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})


async def camera_jpg(_req: web.Request) -> web.Response:
    """The infrared view, for the camera page. Asking for it is what makes sense write it."""
    Path("/tmp/breakpoint_camera.wanted").touch()
    try:
        data = Path("/tmp/breakpoint_camera.jpg").read_bytes()
    except OSError:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def page_jpg(_req: web.Request) -> web.Response:
    """The last page photo, for the thumbnail on the page key."""
    if not shot["path"] or not Path(shot["path"]).is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(shot["path"], headers={"Cache-Control": "no-store"})


async def diagnostics(_req: web.Request) -> web.Response:
    return web.Response(text=DIAGNOSTICS.read_text(), content_type="text/html", headers={"Cache-Control": "no-store"})


async def keypad(_req: web.Request) -> web.Response:
    return web.Response(text=KEYPAD.read_text(), content_type="text/html", headers={"Cache-Control": "no-store"})


async def ws_handler(req: web.Request) -> web.WebSocketResponse:
    global manual_until, last_press, shoulder_off_until, last_app, last_launch
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    clients.add(ws)
    await ws.send_str(json.dumps(snapshot()))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                d = json.loads(msg.data)
                kind = d.get("type")
                if kind == "decide":
                    resolve(d.get("id", ""), d.get("behavior", ""), item_id=d.get("item"))
                elif kind == "state":
                    await on_sense(d)
                elif kind == "manual":
                    manual_until = 0.0 if time.time() < manual_until else time.time() + MANUAL_SECONDS
                    await broadcast()
                elif kind == "press":      # any keypad press: the user is at the desk
                    last_press = time.time()
                elif kind == "summary":
                    overview()
                    await broadcast()
                elif kind == "cycle":
                    await asyncio.get_running_loop().run_in_executor(None, cycle_window, 1 if d.get("dir", 1) > 0 else -1)
                elif kind == "home":
                    await asyncio.get_running_loop().run_in_executor(None, go_home)
                elif kind == "hide_agents":      # the home key: put the agents' terminals away too
                    for sid, sess in sessions.items():
                        wid = windows.get(sess.get("cwd", "").rstrip("/"))
                        if wid:
                            subprocess.Popen(["xdotool", "windowminimize", str(int(wid, 16))],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif kind == "focus":
                    focus(d.get("id", ""))
                elif kind == "ptt":
                    asyncio.create_task(push_to_talk(d))      # transcription takes seconds; keep the keys responsive
                elif kind == "shoulder_off" and "someone else" in (user.get("reason") or ""):
                    # the camera thinks the person at the desk is not the owner, and they pressed the key:
                    # take that as "it is me" and learn the face again
                    await send_all(json.dumps({"type": "relearn"}))
                elif kind == "shoulder_off":
                    shoulder_off_until = 0.0 if time.time() < shoulder_off_until else time.time() + SHOULDER_OFF_SECONDS
                    await broadcast()
                elif kind == "launch":
                    app = d.get("app", "")
                    if app == last_app and time.time() - last_launch < 1.5:
                        continue                      # a doubled key press must not open things twice
                    last_app, last_launch = app, time.time()
                    asyncio.create_task(launch(app))
                elif kind == "paste_page" and shot["path"]:
                    if time.time() - shot.get("pasted", 0) < 1.5:
                        continue                      # a doubled key press must not paste twice
                    shot["pasted"] = time.time()
                    # Claude Code reads a clipboard image through xclip. Without xclip an agent gets the
                    # file's path instead, which it opens itself. Other apps get a normal paste of the image.
                    if front["app"] == "agent" and not shutil.which("xclip"):
                        subprocess.Popen(["xdotool", "type", "--clearmodifiers", "--delay", "4", "--", shot["path"] + " "],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        subprocess.Popen(["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif kind == "app_key" and d.get("action") in APP_KEYS:
                    subprocess.Popen(["xdotool", "key", "--clearmodifiers", APP_KEYS[d["action"]]],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif kind == "snapped" and d.get("path"):      # from sense: the page photo is saved
                    shot["path"], shot["t"] = d["path"], time.time()
                    shot["live_until"] = 0.0
                    # hand it to the clipboard so one key can paste it into the app in front
                    subprocess.Popen(["/usr/bin/python3", str(Path(__file__).with_name("clip_image.py")), d["path"]],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    await broadcast()
                elif kind in ("teach", "snap", "page_live"):
                    if kind == "page_live":
                        shot["live_until"] = time.time() + 6.5
                    await send_all(msg.data, skip=ws)      # pass it on to the camera detector
                elif kind == "open_game" and not game_running():
                    # whack-a-mole takes over the keypad until an arrow button is pressed
                    game["proc"] = subprocess.Popen(["/usr/bin/python3", str(GAME / "whack_a_mole.py")],
                                                    cwd=GAME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                    start_new_session=True)
                    await broadcast()
    finally:
        clients.discard(ws)
    return ws


async def ticker(_app: web.Application):
    async def loop():
        n = 0
        while True:
            await asyncio.sleep(0.5)
            n += 1
            now_front = await asyncio.get_running_loop().run_in_executor(None, detect_front)
            changed = now_front != front
            front.update(now_front)
            if changed or n % 2 == 0:
                await broadcast()
            if n % 2 == 0:
                save_state()
                log_kind()
    task = asyncio.create_task(loop())
    yield
    task.cancel()


ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}


@web.middleware
async def same_origin(req: web.Request, handler):
    """Only our own pages may talk to the hub from a browser; other websites are refused."""
    origin = req.headers.get("Origin")
    if origin and origin not in ALLOWED_ORIGINS:
        raise web.HTTPForbidden()
    return await handler(req)


def save_state() -> None:
    STATE_FILE.write_text(json.dumps({"sessions": sessions, "windows": windows, "idle_misfires": idle_misfires}))


def load_state() -> None:
    global idle_misfires
    try:
        d = json.loads(STATE_FILE.read_text())
    except Exception:
        return
    sessions.update(d.get("sessions", {}))
    for s in sessions.values():          # nothing is held after a restart, so nobody is waiting on a call
        if s.get("state") in ("waiting", "asking"):
            s["state"] = "running"
    windows.update(d.get("windows", {}))
    idle_misfires = d.get("idle_misfires", 0)


def main() -> None:
    names.load_env()
    load_state()
    app = web.Application(middlewares=[same_origin])
    app.add_routes([
        web.post("/hook", hook), web.post("/window", window),
        web.get("/state", state), web.get("/ws", ws_handler), web.get("/timeline", timeline),
        web.get("/", page), web.get("/keypad", keypad), web.get("/diagnostics", diagnostics),
        web.get("/camera", camera), web.get("/camera.jpg", camera_jpg), web.get("/page.png", page_jpg),
        web.static("/vendor", VENDOR_JS),
    ])
    app.cleanup_ctx.append(ticker)
    web.run_app(app, host="127.0.0.1", port=PORT, handler_cancellation=True, print=lambda *_: print(f"hub on http://127.0.0.1:{PORT}/"))


if __name__ == "__main__":
    main()
