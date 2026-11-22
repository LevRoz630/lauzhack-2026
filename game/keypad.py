"""Talk to the Logitech MX Creative Keypad (046d:c354) over raw HID.

Used by the break game: drawing pictures on the nine LCD keys and
reading the keys and the two side buttons.
"""
import glob
import io
import os
import select
import sys
import time
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TILE = 118                    # one key is 118x118 pixels
PITCH = 158                   # distance from one key to the next
ORIGIN = (23, 6)              # where the first key sits on the panel
SIZE = 2 * PITCH + TILE       # 434: the square covering all nine keys
PACKET = 4095
CID_LEFT, CID_RIGHT = 0x01A1, 0x01A2   # the two side buttons
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def font(size):
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def blank(color):
    return Image.new("RGB", (TILE, TILE), color)


def text_tile(text, bg, fg="white", size=40):
    """A key-sized picture with centred text (use \\n for several lines)."""
    img = blank(bg)
    d = ImageDraw.Draw(img)
    lines = text.split("\n")
    f = font(size)
    total = sum(d.textbbox((0, 0), ln, font=f)[3] for ln in lines) + 6 * (len(lines) - 1)
    y = (TILE - total) // 2
    for ln in lines:
        box = d.textbbox((0, 0), ln, font=f)
        d.text(((TILE - box[2]) // 2, y), ln, font=f, fill=fg)
        y += box[3] + 6
    return img


class Keypad:
    """The nine LCD keys. poll() returns pressed keys as 0..8, row by row."""

    def __init__(self):
        self.fd = os.open(self._find(), os.O_RDWR)
        self.pressed = set()
        self.side = set()
        self.events = deque()

    @staticmethod
    def _find():
        for uevent in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
            if "0000046D:0000C354" in Path(uevent).read_text().upper():
                return "/dev/" + uevent.split("/")[4]
        sys.exit("MX Keypad not found. Is it plugged in?")

    def close(self):
        os.close(self.fd)

    # ---- input -----------------------------------------------------------
    def _key_change(self, before, now):
        for key in sorted(now - before):
            self.events.append(key)

    def _side_change(self, before, now):
        pass

    def _read(self, timeout):
        """Read one report. True = draw acknowledged, False = other, None = nothing came."""
        if not select.select([self.fd], [], [], max(timeout, 0))[0]:
            return None
        data = os.read(self.fd, 64)
        if len(data) > 3 and data[3] == 0x2B:
            return True
        if len(data) > 6 and data[0] == 0x13 and data[1:4] == b"\xff\x02\x00" and data[5] == 0x01:
            now = {b - 1 for b in data[6:] if 1 <= b <= 9}
            self._key_change(self.pressed, now)
            self.pressed = now
        elif len(data) > 7 and data[0] == 0x11 and data[2] == getattr(self, "feature", None) and data[3] == 0x00:
            now = set()
            for i in range(4, len(data) - 1, 2):
                cid = int.from_bytes(data[i:i + 2], "big")
                if cid == 0:
                    break
                now.add(cid)
            self._side_change(self.side, now)
            self.side = now
        return False

    def poll(self, timeout):
        """Return the next event, or None after timeout seconds."""
        deadline = time.monotonic() + timeout
        while not self.events:
            left = deadline - time.monotonic()
            if left <= 0 or self._read(left) is None:
                break
        return self.events.popleft() if self.events else None

    def pump(self):
        """Collect everything waiting from the keypad without blocking."""
        while self._read(0) is not None:
            pass

    def flush(self):
        self.pump()
        self.events.clear()

    # ---- output ----------------------------------------------------------
    @staticmethod
    def packets(jpeg, x, y, w, h):
        """Split JPEG bytes into the HID packets that draw them at x, y."""
        packets = []
        first = min(len(jpeg), PACKET - 20)
        head = bytearray(PACKET)
        head[0:4] = b"\x14\xff\x02\x2b"
        head[4] = 0x20 | 0x80 | 1 | (0x40 if first >= len(jpeg) else 0)
        head[5:9] = b"\x01\x00\x01\x00"
        for off, val in ((9, x), (11, y), (13, w), (15, h)):
            head[off:off + 2] = val.to_bytes(2, "big")
        head[17:20] = len(jpeg).to_bytes(3, "big")
        head[20:20 + first] = jpeg[:first]
        packets.append(bytes(head))
        pos, part = first, 2
        while pos < len(jpeg):
            n = min(len(jpeg) - pos, PACKET - 5)
            pkt = bytearray(PACKET)
            pkt[0:4] = b"\x14\xff\x02\x2b"
            pkt[4] = 0x20 | part | (0x40 if pos + n >= len(jpeg) else 0)
            pkt[5:5 + n] = jpeg[pos:pos + n]
            packets.append(bytes(pkt))
            pos += n
            part += 1
        return packets

    @staticmethod
    def encode(img, x, y):
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=85)
        return Keypad.packets(buf.getvalue(), x, y, *img.size)

    @staticmethod
    def tile(img, key):
        """Packets that draw a key-sized picture on key 0..8."""
        row, col = divmod(key, 3)
        return Keypad.encode(img, ORIGIN[0] + col * PITCH, ORIGIN[1] + row * PITCH)

    def send(self, packets):
        """Write packets and wait briefly for the keypad to acknowledge."""
        for pkt in packets:
            os.write(self.fd, pkt)
        deadline = time.monotonic() + 0.3
        while True:
            got = self._read(deadline - time.monotonic())
            if got is None:
                return False
            if got:
                return True

    def draw(self, img, key):
        return self.send(self.tile(img, key))

    def frame(self, img):
        """Draw one 434x434 picture across all nine keys."""
        return self.send(self.encode(img, *ORIGIN))

    def clear(self):
        self.frame(Image.new("RGB", (SIZE, SIZE), (0, 0, 0)))


class SidePad(Keypad):
    """Keypad that also reports the two side buttons, as 'L' and 'R'."""

    def __init__(self):
        super().__init__()
        self.feature = self._feature_index(0x1B04) or 0x0B
        self._divert(True)

    def _hidpp(self, feature, function, params=b""):
        msg = bytes([0x11, 0xFF, feature, (function << 4) | 0x0B]) + params
        os.write(self.fd, msg.ljust(20, b"\x00"))
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if not select.select([self.fd], [], [], 0.1)[0]:
                continue
            data = os.read(self.fd, 64)
            if data[0] == 0x11 and data[2] == feature and data[3] == msg[3]:
                return data
        return None

    def _feature_index(self, feature_id):
        reply = self._hidpp(0x00, 0, feature_id.to_bytes(2, "big"))
        return reply[4] if reply else None

    def _divert(self, on):
        for cid in (CID_LEFT, CID_RIGHT):
            self._hidpp(self.feature, 3, cid.to_bytes(2, "big") + bytes([0x03 if on else 0x02]))

    def _side_change(self, before, now):
        for cid in now - before:
            self.events.append("L" if cid == CID_LEFT else "R")

    def close(self):
        try:
            self._divert(False)
        finally:
            super().close()
