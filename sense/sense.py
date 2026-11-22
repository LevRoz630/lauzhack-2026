"""Sense: camera to user state, sent to the hub.

Run:  uv run python -m sense.sense            (add --show to print features)
States: focused, available, shoulder, away, distracted (phone in hand, from the taught desk view).
Available is never inferred from posture, only from breakpoints (return from away, a visitor leaving).
No frame ever leaves this process; only the state and a few numbers do.
"""
import argparse
import asyncio
import glob
import json
import math
import statistics
import time
from pathlib import Path

import aiohttp
import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mpp
from mediapipe.tasks.python import vision

from .desk import Desk
from .owner import Owner

HUB = "http://127.0.0.1:8770/ws"
MODEL = Path(__file__).resolve().parent.parent / "models" / "face_landmarker.task"

# thresholds
YAW_AWAY = 25.0        # degrees from baseline
PITCH_AWAY = 14.0
LEAN_BACK = 0.82       # face width relative to baseline
SECOND_FACE = 0.3      # a second face counts only if at least this fraction of the main face's width
SECOND_YAW = 35.0      # a second face looks at the screen if its absolute yaw and pitch (degrees) are both under these
SECOND_PITCH = 35.0
TYPING_IDLE = 3.0      # seconds since last key or mouse input
MIN_BREAK = 6.0          # how long Away or Shoulder must have lasted for the return to count as a breakpoint
NEAR_FACE = 0.5          # a face smaller than this fraction of the user's usual size is someone in the background
STRANGER_SECONDS = 2.5   # how long only non-owner faces must be seen before the screen is covered
OWNER_MAX_YAW = 30.0     # beyond this head turn the face is not compared with the owner
HOLD = {"away": 3.0, "shoulder": 1.5, "focused": 1.0}
BREAK_SECONDS = 20.0   # how long a breakpoint keeps the user "available"


def find_camera(name: str) -> str | None:
    for p in sorted(glob.glob("/sys/class/video4linux/video*")):
        if Path(p, "name").read_text().strip().endswith(name) and Path(p, "index").read_text().strip() == "0":
            return "/dev/" + Path(p).name
    return None


def open_camera() -> cv2.VideoCapture:
    dev = find_camera("LVNext Camera")
    if dev is None:
        raise SystemExit("Logitech camera not found")
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    # 640x480 uncompressed: higher resolutions stall on a USB 2 link shared with the infrared camera
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


PAGES = Path.home() / "Pictures" / "pages"
LIVE = "/tmp/breakpoint_page_live.png"     # small local preview, shown by the overlay while a page is held up
SHOT = "/tmp/breakpoint_page_shot.png"     # the photo just taken, shown by the overlay for a few seconds


def take_still(cap: cv2.VideoCapture) -> tuple[str, cv2.VideoCapture]:
    """Switch the colour camera to full resolution, keep the sharpest of a few frames, switch back."""
    cap.release()
    time.sleep(0.4)       # the camera needs a moment between two stream formats
    dev = find_camera("LVNext Camera")
    still = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    still.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    still.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    still.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    best, best_score = None, -1.0
    deadline = time.time() + 5
    for i in range(14):
        if time.time() > deadline:
            break
        ok, frame = still.read()
        if not ok or i < 6:        # the first frames are still adjusting exposure
            continue
        score = cv2.Laplacian(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        if score > best_score:
            best, best_score = frame, score
    still.release()
    time.sleep(0.4)
    path = ""
    if best is not None:
        PAGES.mkdir(parents=True, exist_ok=True)
        path = str(PAGES / time.strftime("page-%Y%m%d-%H%M%S.png"))
        cv2.imwrite(path, best)
        cv2.imwrite(SHOT, cv2.resize(best, (480, 270)))
    return path, open_camera()


class Idle:
    """Seconds since the last keyboard or mouse input, from the X server."""

    def __init__(self) -> None:
        try:
            from Xlib import display
            self.d = display.Display()
            self.root = self.d.screen().root
            self.root.screensaver_query_info()   # raises if the extension is missing
        except Exception:
            self.d = None

    def seconds(self) -> float:
        if self.d is None:
            return 0.0
        try:
            return self.root.screensaver_query_info().idle / 1000.0
        except Exception:
            return 0.0


def face_metrics(landmarks, matrix) -> dict:
    xs = [p.x for p in landmarks]
    width = max(xs) - min(xs)
    r = np.array(matrix)[:3, :3]
    pitch = math.degrees(math.atan2(-r[1, 2], r[2, 2]))
    yaw = math.degrees(math.asin(max(-1.0, min(1.0, r[0, 2]))))
    # mouth opening: inner lips apart, relative to face height, so distance to the camera cancels out
    lip = math.dist((landmarks[13].x, landmarks[13].y), (landmarks[14].x, landmarks[14].y))
    face = math.dist((landmarks[10].x, landmarks[10].y), (landmarks[152].x, landmarks[152].y)) or 1.0
    return {"width": width, "yaw": yaw, "pitch": pitch, "mouth": lip / face}


class Rules:
    def __init__(self) -> None:
        self.base = None            # baseline yaw, pitch, width while facing the screen
        self.samples: list[dict] = []
        self.state = "focused"
        self.reason = "starting"
        self.candidate = None
        self.since = 0.0
        self.stranger_since = 0.0
        self.state_since = 0.0
        self.break_until = 0.0      # a natural breakpoint just happened: report available until then
        self.break_reason = ""

    def calibrate(self, m: dict, typing: bool) -> None:
        # Typing implies facing the screen, so those frames define "facing the screen".
        if self.base is None or typing:
            self.samples.append(m)
            self.samples = self.samples[-150:]
            if len(self.samples) >= 20:
                self.base = {k: statistics.median(s[k] for s in self.samples) for k in ("yaw", "pitch", "width")}

    def raw(self, faces: list[dict], idle: float, now: float = 0.0) -> tuple[str, str]:
        if not faces:
            # Someone using the keyboard or mouse is here, even if the camera cannot see a face
            # (looking at another screen, leaning out of frame). Away needs both: no face and no input.
            if idle < TYPING_IDLE:
                return "focused", "typing, face out of view"
            return "away", "no face"
        # People in the background do not make the user present: without a face of roughly the user's
        # usual size, the desk is empty.
        if self.base and not any(f["width"] >= NEAR_FACE * self.base["width"] for f in faces):
            if idle < TYPING_IDLE:
                return "focused", "typing, face out of view"
            return "away", "nobody at the desk"
        owners = [f for f in faces if f.get("owner")]
        if not owners and all(f.get("owner") is False for f in faces):
            # The owner is known and is not among these faces. Only after it has been so for a while:
            # one bad frame of the owner must not cover the screen.
            self.stranger_since = self.stranger_since or now
            if now - self.stranger_since >= STRANGER_SECONDS:
                return "shoulder", "someone else is at your desk"
        else:
            self.stranger_since = 0.0
        main = max(owners or faces, key=lambda f: f["width"])
        others = [f for f in faces if f is not main and f["width"] >= SECOND_FACE * main["width"]
                  and abs(f["yaw"]) < SECOND_YAW and abs(f["pitch"]) < SECOND_PITCH]
        if others:
            return "shoulder", "someone is looking at your screen"
        typing = idle < TYPING_IDLE
        self.calibrate(main, typing)
        if typing:
            return "focused", "typing"
        if self.base is None:
            return "focused", "calibrating"
        dyaw, dpitch = main["yaw"] - self.base["yaw"], main["pitch"] - self.base["pitch"]
        lean = main["width"] / self.base["width"]
        # Not typing is not the same as free: reading and thinking stay focused.
        if abs(dyaw) > YAW_AWAY:
            return "focused", "looking away, thinking"
        if abs(dpitch) > PITCH_AWAY:
            return "focused", "looking down, thinking" if dpitch > 0 else "looking up, thinking"
        if lean < LEAN_BACK:
            return "focused", "leaning back, thinking"
        return "focused", "eyes on screen, reading"

    def step(self, faces: list[dict], idle: float, now: float) -> bool:
        """Returns True when the debounced state or its reason changed."""
        raw, reason = self.raw(faces, idle, now)
        if raw == self.state:
            self.candidate = None
            changed = reason != self.reason
            self.reason = reason
            return changed
        if raw != self.candidate:
            self.candidate, self.since = raw, now
        if now - self.since >= HOLD[raw]:
            # a breakpoint only after a real absence or visit, not after a glance down at the keypad
            if raw == "focused" and self.state in ("away", "shoulder") and now - self.state_since >= MIN_BREAK:
                self.break_until = now + BREAK_SECONDS
                self.break_reason = "back at the desk" if self.state == "away" else "visitor left"
            self.state, self.reason, self.candidate = raw, reason, None
            self.state_since = now
            return True
        return False

    def reported(self, now: float) -> tuple[str, str]:
        """Breakpoints are the only automatic release: coming back, or a visitor leaving."""
        if self.state == "focused" and now < self.break_until:
            return "available", self.break_reason
        return self.state, self.reason


async def run(show: bool) -> None:
    det = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=str(MODEL)), num_faces=3,
        output_facial_transformation_matrixes=True, running_mode=vision.RunningMode.VIDEO))
    cap = open_camera()
    idle, rules = Idle(), Rules()
    desk = Desk(find_camera("CPD Camera"))
    owner = Owner()
    t0, last_sent, n = time.time(), 0.0, 0
    lips: list = []
    recent: list = []     # last second of mouth openings
    loop = asyncio.get_running_loop()
    want = {"snap": False, "live_until": 0.0}

    async def listen(sock) -> None:
        async for m in sock:
            if m.type == aiohttp.WSMsgType.TEXT:
                d = json.loads(m.data)
                if d.get("type") == "teach":
                    desk.teach(d.get("label", ""))
                elif d.get("type") == "snap":
                    want["snap"] = True
                elif d.get("type") == "relearn":      # "that is me": forget the remembered face and learn it again
                    owner.samples, owner.ref = [], None
                    rules.stranger_since = 0.0
                    want["relearn_until"] = time.time() + 10      # learn from the next frames without needing typing
                elif d.get("type") == "page_live":
                    want["live_until"] = time.time() + 7

    async with aiohttp.ClientSession() as http:
        ws = None
        while True:
            if want["snap"]:      # the page key: one full-resolution photo, kept on this laptop
                want["snap"] = False
                desk.paused = True
                await asyncio.sleep(0.5)
                path, cap = await loop.run_in_executor(None, take_still, cap)
                desk.paused = False
                if ws is not None and not ws.closed and path:
                    await ws.send_str(json.dumps({"type": "snapped", "path": path}))
                continue
            ok, frame = await loop.run_in_executor(None, cap.read)
            if not ok:      # camera unplugged or moved to another port: reopen it
                cap.release()
                try:
                    cap = open_camera()
                except SystemExit:
                    await asyncio.sleep(1)
                continue
            n += 1
            if n % 3 == 0 and time.time() < want["live_until"]:      # live preview while a page is held up
                cv2.imwrite(LIVE + ".tmp.png", cv2.resize(frame, (480, 360)))
                Path(LIVE + ".tmp.png").replace(LIVE)
            if n % 3:      # 30 fps camera, 10 fps analysis
                continue
            now = time.time()
            img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            res = det.detect_for_video(img, int((now - t0) * 1000))
            faces = [face_metrics(lm, mx) for lm, mx in zip(res.face_landmarks, res.facial_transformation_matrixes)]
            secs = idle.seconds()
            if owner.model is not None:
                vecs = [owner.embed(frame, lm) for lm in res.face_landmarks]
                # one person, facing the camera, typing or having just said "that is me": that is the owner
                if (not owner.enrolled() and len(vecs) == 1 and vecs[0] is not None
                        and abs(faces[0]["yaw"]) < 20 and (secs < 1.0 or time.time() < want.get("relearn_until", 0))):
                    owner.enrol(vecs[0])
                for f, v in zip(faces, vecs):
                    # a face turned far from the camera cannot be recognised reliably: leave it undecided
                    f["owner"] = owner.is_owner(v) if abs(f["yaw"]) < OWNER_MAX_YAW else None
            changed = rules.step(faces, secs, now)
            if faces:
                lips.append([round(now, 3), round(max(faces, key=lambda f: f["width"])["mouth"], 4)])
                recent.append(lips[-1][1])
                del recent[:-10]
            if n % 15 == 0:
                known = "other" if secs < 1.0 or (rules.state == "focused" and rules.reason.startswith("eyes on screen")) else None
                desk.tick(known)
            if changed or now - last_sent > 0.5:
                last_sent = now
                main = max(faces, key=lambda f: f["width"]) if faces else None
                st, why = rules.reported(now)
                if st == "focused" and desk.taught() and desk.label == "phone" and secs >= TYPING_IDLE:
                    st, why = "distracted", "attention elsewhere"
                msg = {"type": "state", "state": st, "reason": why,
                       "desk": desk.status(),
                       "lips": lips[-20:], "features": {
                    "faces": len(faces), "idle_s": round(secs, 1),
                    "yaw": round(main["yaw"] - rules.base["yaw"], 1) if main and rules.base else None,
                    "pitch": round(main["pitch"] - rules.base["pitch"], 1) if main and rules.base else None,
                    "lean": round(main["width"] / rules.base["width"], 2) if main and rules.base else None,
                    "lip_move": round(statistics.pstdev(recent), 4) if len(recent) >= 3 and faces else None}}
                lips.clear()
                if show:
                    print(json.dumps(msg), flush=True)
                try:
                    if ws is None or ws.closed:
                        ws = await http.ws_connect(HUB)
                        asyncio.create_task(listen(ws))
                    await ws.send_str(json.dumps(msg))
                except Exception:
                    ws = None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="print each message")
    asyncio.run(run(ap.parse_args().show))
