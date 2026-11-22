"""Who is the owner: remembers the user's face for this run only.

The first frames in which one person is typing are taken to be the owner. Their face is turned into
a vector by a pretrained recognition model and kept in memory; nothing is written to disk, and it
is forgotten when the process stops. Other faces are compared against it.
"""
from pathlib import Path

import cv2
import numpy as np

MODEL = Path(__file__).resolve().parent.parent / "models" / "face_recognition_sface.onnx"
# where the eyes, nose and mouth corners must land in the 112x112 crop the model expects
TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)
ENROL_FRAMES = 8
SAME_PERSON = 0.40     # cosine similarity; the model's own recommended cut is 0.363, the same face scored 0.68 to 0.77 here


class Owner:
    def __init__(self) -> None:
        self.model = cv2.FaceRecognizerSF.create(str(MODEL), "") if MODEL.exists() else None
        self.samples: list = []
        self.ref = None

    def enrolled(self) -> bool:
        return self.ref is not None

    def embed(self, frame, landmarks):
        h, w = frame.shape[:2]

        def at(i):
            return (landmarks[i].x * w, landmarks[i].y * h)

        eyes, mouth = sorted([at(468), at(473)]), sorted([at(61), at(291)])
        src = np.array([eyes[0], eyes[1], at(1), mouth[0], mouth[1]], dtype=np.float32)
        matrix, _ = cv2.estimateAffinePartial2D(src, TEMPLATE)
        if matrix is None:
            return None
        vec = self.model.feature(cv2.warpAffine(frame, matrix, (112, 112))).flatten()
        return vec / (np.linalg.norm(vec) or 1.0)

    def enrol(self, vec) -> None:
        self.samples.append(vec)
        if len(self.samples) >= ENROL_FRAMES:
            ref = np.mean(self.samples, axis=0)
            self.ref = ref / (np.linalg.norm(ref) or 1.0)

    def is_owner(self, vec) -> bool | None:
        """True or False once the owner is known; None before that."""
        if self.ref is None or vec is None:
            return None
        return float(vec @ self.ref) >= SAME_PERSON
