"""Desk view: is there a phone in the hands, from the infrared camera.

A pretrained image embedder turns the lower half of the infrared frame into a vector.
The user teaches a few seconds of the phone; everything seen while typing or reading the screen
is collected by itself as "other". New frames are labelled by nearest neighbours.
"""
import threading
import time
from collections import Counter, deque
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mpp
from mediapipe.tasks.python import vision

MODELS = Path(__file__).resolve().parent.parent / "models"
EXAMPLES = MODELS / "desk_examples.npz"
PREVIEW = Path("/tmp/breakpoint_camera.jpg")             # only written while the camera page is open
PREVIEW_WANTED = Path("/tmp/breakpoint_camera.wanted")
LABELS = ("phone", "other")
TEACH_SECONDS = 10.0
AUTO_MAX = 60        # counterexamples collected by themselves
AUTO_EVERY = 2.0     # seconds between them, so they are not near-copies
K = 5


class Desk:
    def __init__(self, device: str | None) -> None:
        self.embedder = vision.ImageEmbedder.create_from_options(vision.ImageEmbedderOptions(
            base_options=mpp.BaseOptions(model_asset_path=str(MODELS / "image_embedder.tflite")), l2_normalize=True))
        self.examples: dict[str, list] = {label: [] for label in LABELS}
        if EXAMPLES.exists():
            data = np.load(EXAMPLES)
            for label in LABELS:
                if label in data:
                    self.examples[label] = list(data[label])
        self.votes: deque = deque(maxlen=6)
        self.label = None
        self.teaching, self.teach_until = None, 0.0
        self.auto_at = 0.0
        self.paused = False
        self.auto_n = 0
        self.preview_at = 0.0
        self.frame = None
        self.device = device
        if device:
            threading.Thread(target=self._capture, daemon=True).start()

    def _capture(self) -> None:
        cap = None
        while True:
            if self.paused:        # the colour camera needs the whole USB link for a full-resolution photo
                if cap is not None:
                    cap.release()
                    cap = None
                time.sleep(0.1)
                continue
            if cap is None:
                cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 512)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 640)
            ok, frame = cap.read()
            if ok:
                self.frame = frame
                self._preview(frame)
            else:
                cap.release()
                cap = None
                time.sleep(1)

    def _preview(self, frame) -> None:
        """While the camera page is open, write the current frame to a local file for it, five times a second."""
        now = time.time()
        try:
            wanted = now - PREVIEW_WANTED.stat().st_mtime < 3
        except OSError:
            return
        if wanted and now - self.preview_at >= 0.2:
            self.preview_at = now
            shown = frame.copy()
            cut = int(shown.shape[0] * 0.5)
            cv2.line(shown, (0, cut), (shown.shape[1], cut), (0, 255, 0), 1)      # the phone check uses what is below
            cv2.imwrite(str(PREVIEW) + ".tmp.jpg", shown)
            Path(str(PREVIEW) + ".tmp.jpg").replace(PREVIEW)

    def teach(self, label: str) -> None:
        if label == "clear":
            self.examples = {name: [] for name in LABELS}
            EXAMPLES.unlink(missing_ok=True)
            self.votes.clear()
            self.label = None
        elif label == "fresh":        # forget the no-phone examples; they are collected again from the present scene
            self.examples["other"] = []
            self.votes.clear()
            self.label = None
        elif label in LABELS:
            self.teaching, self.teach_until = label, time.time() + TEACH_SECONDS

    def taught(self) -> bool:
        return all(self.examples[label] for label in LABELS)

    def tick(self, known: str | None = None) -> None:
        """Call about twice a second. `known` is "other" when the caller is sure of it right now (the user
        is typing, or reading the screen); such frames are collected slowly as counterexamples."""
        frame = self.frame
        if frame is None:
            return
        crop = frame[int(frame.shape[0] * 0.5):]
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        vec = np.array(self.embedder.embed(image).embeddings[0].embedding, dtype=np.float32)
        now = time.time()
        if self.teaching:
            if now < self.teach_until:
                self.examples[self.teaching].append(vec)
                return
            self.teaching = None
            np.savez(EXAMPLES, **{k: np.array(v) for k, v in self.examples.items() if v})
        if known in LABELS and now - self.auto_at >= AUTO_EVERY:
            # A rolling window: the oldest go, so the counterexamples always show the desk as it is now.
            # Stale ones from an earlier camera position made everything look like the phone.
            self.examples[known].append(vec)
            del self.examples[known][:-AUTO_MAX]
            self.auto_at = now
            self.auto_n += 1
            if self.auto_n % 10 == 0:
                np.savez(EXAMPLES, **{k: np.array(v) for k, v in self.examples.items() if v})
        if not self.taught():
            return
        scored = sorted(((float(vec @ ex), label) for label, exs in self.examples.items() for ex in exs), reverse=True)[:K]
        top = Counter(label for _, label in scored).most_common(1)[0][0]
        self.votes.append(top)
        self.label = Counter(self.votes).most_common(1)[0][0]

    def status(self) -> dict:
        left = max(0, round(self.teach_until - time.time())) if self.teaching else 0
        return {"desk": self.label, "teaching": self.teaching, "teach_left": left,
                "examples": {k: len(v) for k, v in self.examples.items()}}
