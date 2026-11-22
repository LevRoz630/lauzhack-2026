"""Push-to-talk: record from the camera's microphone while a key is held, transcribe on the laptop.

The audio never leaves this machine. Only the transcript is passed on to the agent.
"""
import os
import time
import signal
import subprocess
import tempfile
import threading

import numpy as np

MODEL = os.environ.get("STT_MODEL", "small.en")   # about 1.5 s per phrase on mains power; tiny.en is the fallback on battery
RATE = 16000
LIP_MOVE = float(os.environ.get("LIP_MOVE", "0.006"))   # mouth-opening variation that counts as speaking
LIP_PAD = 0.35     # seconds kept either side of lip movement
# Words the model should expect, so developer terms are spelled right.
HINT = ("Developer dictating to a coding agent: Claude, Python, npm, pnpm, git, commit, README, function, "
        "refactor, pytest, API, JSON, endpoint, install, dependency.")
MAX_SECONDS = 180
RECORDING = os.path.join(tempfile.gettempdir(), "breakpoint_ptt.wav")


def mic() -> str:
    """The camera's microphone when it is plugged in, otherwise the laptop's own."""
    if os.environ.get("PTT_MIC"):
        return os.environ["PTT_MIC"]
    cards = subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
    return "plughw:CARD=LVNext" if "LVNext" in cards else "default"


def stop_strays() -> None:
    """End any recorder left over from an earlier run of the hub, and delete what it wrote."""
    subprocess.run(["pkill", "-x", "arecord"], check=False)
    try:
        os.remove(RECORDING)
    except OSError:
        pass


class Voice:
    def __init__(self) -> None:
        self.model = None
        self.proc = None
        self.t0 = 0.0
        self.path = RECORDING
        stop_strays()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        from faster_whisper import WhisperModel
        self.model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=4)

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None                 # the last recording hit its time limit and ended by itself
        if self.proc is None:
            stop_strays()
            self.t0 = time.time()
            # -d caps a recording at MAX_SECONDS, so one that is never stopped cannot keep the microphone
            self.proc = subprocess.Popen(["arecord", "-q", "-D", mic(), "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                                          "-d", str(MAX_SECONDS), self.path],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _end(self) -> None:
        if self.proc is not None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def cancel(self) -> None:
        self._end()
        try:
            os.remove(self.path)             # a cancelled recording is not kept either
        except OSError:
            pass

    def stop(self, lips: list | None = None) -> tuple[str, str]:
        """Stop recording and return (transcript, note). Blocking; run it off the event loop.

        lips is a list of [time, mouth opening] samples from the camera. Audio is kept only where
        the user's lips were moving, so other people's voices are dropped.
        """
        t1 = time.time()
        self._end()
        if self.model is None:
            return "", "speech model still loading"
        try:
            raw = open(self.path, "rb").read()[44:]     # skip the WAV header
            os.remove(self.path)                        # the recording is not kept
        except OSError:
            return "", "no recording"
        audio = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768
        note = ""
        spans = lip_spans(lips or [], self.t0, t1)
        if spans is not None:
            kept = [audio[int(a * RATE): int(b * RATE)] for a, b in spans]
            audio = np.concatenate(kept) if kept else audio[:0]
            note = "lip gate" if kept else "lips did not move"
        if len(audio) < RATE * 0.3:
            return "", note or "too short"
        segments, _ = self.model.transcribe(audio, language="en", beam_size=5, initial_prompt=HINT,
                                            condition_on_previous_text=False, without_timestamps=True)
        return " ".join(seg.text.strip() for seg in segments).strip(), note


def lip_spans(lips: list, t0: float, t1: float) -> list | None:
    """Stretches of the recording (seconds from its start) during which the lips moved.

    None means the camera gave too little to judge, so nothing is cut.
    """
    pts = [(t, v) for t, v in lips if t0 - 0.6 <= t <= t1 + 0.6]
    if len(pts) < 5:
        return None
    spans: list = []
    for i in range(len(pts)):
        window = [v for t, v in pts if abs(t - pts[i][0]) <= 0.3]
        if len(window) >= 3 and float(np.std(window)) > LIP_MOVE:
            a, b = max(0.0, pts[i][0] - t0 - LIP_PAD), min(t1 - t0, pts[i][0] - t0 + LIP_PAD)
            if spans and a <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], b)
            elif b > a:
                spans.append([a, b])
    return spans
