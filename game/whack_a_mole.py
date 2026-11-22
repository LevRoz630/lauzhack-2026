#!/usr/bin/env python3
"""Whack-a-mole on the Logitech MX Creative Keypad.

Faster hits score more: every hit is multiplied by a speed bonus from x1.0
(just in time) to x3.0 (instant), and by a streak bonus that grows with every
hit in a row.

    brown mole     hit it                        100 points
    gold mole      quick! it leaves early        300 points, no penalty if missed
    helmet mole    needs two hits                150 points
    purple mole    jumps to another key          200 points
    bomb           do NOT hit it                 costs a life
    heart          hit it                        gives a life back

A mole that gets away, a wrong key or a bomb costs a life and the streak.
Every 10 hits is a new level: faster, more moles at once, and a mole rush.

Run:  python3 whack_a_mole.py
Quit with either arrow button under the keys, or Ctrl+C.
"""
import random
import signal
import time
from pathlib import Path

from PIL import ImageDraw

from keypad import SidePad, blank, text_tile


class Pad(SidePad):
    """The keypad for this game: either arrow button under the keys quits."""

    def poll(self, timeout):
        event = super().poll(timeout)
        if isinstance(event, str):
            raise KeyboardInterrupt
        return event


SCORE_FILE = Path.home() / ".local/share/keypad-games/whack_highscore"
GRASS, DARK = (34, 110, 40), (15, 15, 15)
BASE_POINTS = {"mole": 100, "gold": 300, "tough": 150, "jumper": 200}
COSTS_LIFE_IF_MISSED = ("mole", "tough", "jumper")
MAX_LIVES = 5
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
NEIGHBOURS = {k: [n for n in range(9) if n != k and abs(n // 3 - k // 3) <= 1 and abs(n % 3 - k % 3) <= 1]
              for k in range(9)}


# ---- pictures --------------------------------------------------------------
def hole_tile():
    img = blank(GRASS)
    ImageDraw.Draw(img).ellipse((20, 62, 98, 104), fill=(25, 40, 20))
    return img


def mole_tile(body=(133, 84, 45), helmet=False, dizzy=False):
    img = hole_tile()
    d = ImageDraw.Draw(img)
    d.ellipse((24, 14, 94, 104), fill=body)
    d.rectangle((24, 60, 94, 96), fill=body)
    if dizzy:
        for x in (38, 66):
            d.line((x, 36, x + 14, 50), fill="black", width=4)
            d.line((x, 50, x + 14, 36), fill="black", width=4)
    else:
        d.ellipse((38, 36, 52, 50), fill="white")
        d.ellipse((66, 36, 80, 50), fill="white")
        d.ellipse((43, 40, 50, 47), fill="black")
        d.ellipse((71, 40, 78, 47), fill="black")
    d.ellipse((50, 54, 68, 68), fill=(240, 130, 150))
    d.rectangle((54, 72, 58, 82), fill="white")
    d.rectangle((60, 72, 64, 82), fill="white")
    if helmet:
        d.pieslice((20, 6, 98, 70), 180, 360, fill=(150, 155, 165))
        d.rectangle((16, 34, 102, 42), fill=(110, 115, 125))
    return img


def bomb_tile():
    img = blank(GRASS)
    d = ImageDraw.Draw(img)
    d.ellipse((24, 34, 94, 104), fill=(20, 20, 20))
    d.line((72, 40, 86, 20), fill=(190, 150, 90), width=5)
    d.ellipse((80, 8, 96, 24), fill=(255, 180, 0))
    d.ellipse((40, 48, 54, 62), fill=(90, 90, 90))
    return img


def heart_tile():
    img = blank(GRASS)
    d = ImageDraw.Draw(img)
    red = (230, 40, 60)
    d.ellipse((22, 26, 62, 66), fill=red)
    d.ellipse((56, 26, 96, 66), fill=red)
    d.polygon(((25, 56), (93, 56), (59, 100)), fill=red)
    return img


ART = {
    "hole": hole_tile,
    "mole": mole_tile,
    "gold": lambda: mole_tile(body=(240, 195, 40)),
    "tough": lambda: mole_tile(helmet=True),
    "dizzy": lambda: mole_tile(dizzy=True),
    "jumper": lambda: mole_tile(body=(150, 85, 195)),
    "bomb": bomb_tile,
    "heart": heart_tile,
    "boom": lambda: text_tile("BOOM", (200, 20, 20), "white", 32),
    "miss": lambda: text_tile("X", (110, 30, 30), "white", 60),
    "life": lambda: text_tile("+♥", (230, 40, 60), "white", 44),
    "clang": lambda: text_tile("CLANG", (150, 155, 165), "black", 26),
}


# ---- high score ------------------------------------------------------------
def load_best():
    try:
        return int(SCORE_FILE.read_text())
    except (OSError, ValueError):
        return 0


def save_best(score):
    SCORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCORE_FILE.write_text(str(score))


# ---- game ------------------------------------------------------------------
class Game:
    def __init__(self, pad, best):
        self.pad = pad
        self.best = best
        self.art = {name: [Pad.tile(make(), k) for k in range(9)] for name, make in ART.items()}
        self.active = {}    # key -> critter
        self.flashes = {}   # key -> time the flash ends
        self.score = 0
        self.lives = 3
        self.hits = 0
        self.streak = 0
        self.best_streak = 0
        self.level = 1
        self.reaction_total = 0.0
        self.last_bonus = 1.0
        self.next_spawn = 0.0

    # how the game speeds up
    def window(self):
        return max(0.45, 1.4 * 0.9 ** (self.level - 1))

    def max_at_once(self):
        return min(1 + self.level // 2, 4)

    def spawn_gap(self):
        return random.uniform(0.25, 0.7) * 0.93 ** (self.level - 1)

    def pick_kind(self):
        choices = {"mole": 60}
        if self.level >= 2:
            choices.update(bomb=14, gold=9)
        if self.level >= 3:
            choices["tough"] = 10
        if self.level >= 4:
            choices["jumper"] = 10
        if self.lives < 3:
            choices["heart"] = 6
        return random.choices(list(choices), weights=list(choices.values()))[0]

    # drawing
    def show(self, name, key):
        self.pad.send(self.art[name][key])

    def flash(self, key, name=None, img=None, seconds=0.22):
        if img is not None:
            self.pad.draw(img, key)
        else:
            self.show(name, key)
        self.flashes[key] = time.monotonic() + seconds

    def status(self):
        print(f"\r  score {self.score:6d}   lives {'♥' * self.lives:<5}   level {self.level:2d}   "
              f"streak {self.streak:3d}   last hit x{self.last_bonus:.1f}   best {self.best:6d} ",
              end="", flush=True)

    # critters
    def free_keys(self):
        return [k for k in range(9) if k not in self.active and k not in self.flashes]

    def spawn(self, kind, key):
        window = self.window() * {"gold": 0.55, "bomb": 1.2, "heart": 0.8}.get(kind, 1.0)
        self.show(kind, key)
        now = time.monotonic()
        self.active[key] = {"kind": kind, "born": now, "expires": now + window,
                            "window": window, "jumped": False}

    def rush(self):
        """Three moles at once, in a row, column or diagonal."""
        lines = [line for line in LINES if all(k in self.free_keys() for k in line)]
        if lines:
            for key in random.choice(lines):
                self.spawn("mole", key)

    def lose_life(self):
        self.lives -= 1
        self.streak = 0

    def update(self, now):
        for key, until in list(self.flashes.items()):
            if now >= until:
                del self.flashes[key]
                self.show("hole", key)
        for key, c in list(self.active.items()):
            if c["kind"] == "jumper" and not c["jumped"] and now >= c["born"] + c["window"] / 2:
                spots = [k for k in NEIGHBOURS[key] if k in self.free_keys()]
                if spots:
                    new = random.choice(spots)
                    c["jumped"] = True
                    del self.active[key]
                    self.show("hole", key)
                    self.show("jumper", new)
                    self.active[new] = c
                    continue
            if now >= c["expires"]:
                del self.active[key]
                if c["kind"] in COSTS_LIFE_IF_MISSED:
                    self.lose_life()
                    self.flash(key, "miss")
                else:
                    self.show("hole", key)
                self.status()
        if now >= self.next_spawn:
            free = self.free_keys()
            if free and len(self.active) < self.max_at_once():
                if self.level >= 3 and not self.active and random.random() < 0.05:
                    self.rush()
                else:
                    self.spawn(self.pick_kind(), random.choice(free))
            self.next_spawn = time.monotonic() + self.spawn_gap()

    def press(self, key):
        now = time.monotonic()
        c = self.active.get(key)
        if c is None:
            if key not in self.flashes:   # a second tap on a key that just flashed is forgiven
                self.lose_life()
                self.flash(key, "miss")
        elif c["kind"] == "bomb":
            del self.active[key]
            self.lose_life()
            self.flash(key, "boom", seconds=0.35)
        elif c["kind"] == "heart":
            del self.active[key]
            self.lives = min(self.lives + 1, MAX_LIVES)
            self.flash(key, "life")
        elif c["kind"] == "tough":
            # first hit knocks the helmet off; it needs one more
            self.show("clang", key)
            time.sleep(0.08)
            self.show("dizzy", key)
            now = time.monotonic()
            c.update(kind="mole", born=now, expires=now + c["window"] * 0.8, window=c["window"] * 0.8, base=150)
        else:
            del self.active[key]
            reaction = now - c["born"]
            speed_bonus = max(1.0, min(3.0, 3.0 - 2.0 * reaction / c["window"]))
            streak_bonus = 1 + min(self.streak, 20) * 0.05
            points = round(c.get("base", BASE_POINTS[c["kind"]]) * speed_bonus * streak_bonus)
            self.score += points
            self.hits += 1
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            self.reaction_total += reaction
            self.last_bonus = speed_bonus
            color = (255, 210, 0) if speed_bonus >= 2.3 else (240, 240, 240) if speed_bonus >= 1.6 else (170, 170, 170)
            self.flash(key, img=text_tile(f"+{points}\nx{speed_bonus:.1f}", DARK, color, 28), seconds=0.3)
            if self.hits % 10 == 0:
                self.level_up()
        self.status()

    def level_up(self):
        self.level += 1
        for key in list(self.active):
            del self.active[key]
            self.show("hole", key)
        self.flash(4, img=text_tile(f"LEVEL\n{self.level}", (25, 80, 150), size=28), seconds=0.7)
        time.sleep(0.7)
        self.update(time.monotonic())
        self.pad.flush()
        self.rush()
        self.next_spawn = time.monotonic() + self.window()

    def run(self):
        for k in range(9):
            self.show("hole", k)
        for n in ("3", "2", "1"):
            self.pad.draw(text_tile(n, DARK, size=64), 4)
            time.sleep(0.6)
        self.show("hole", 4)
        self.pad.flush()
        self.next_spawn = time.monotonic() + 0.4
        self.status()
        while self.lives > 0:
            now = time.monotonic()
            self.update(now)
            deadlines = [self.next_spawn, *self.flashes.values(), *(c["expires"] for c in self.active.values())]
            key = self.pad.poll(max(0.0, min(min(deadlines) - time.monotonic(), 0.05)))
            if key is not None:
                self.press(key)
        print()
        return self.score


def show_tiles(pad, tiles):
    for key, img in enumerate(tiles):
        pad.draw(img, key)


def wait_for_press(pad):
    pad.flush()
    while pad.poll(1.0) is None:
        pass


def title_screen(pad, best):
    tiles = [
        text_tile("WH", GRASS), text_tile("AC", GRASS), text_tile("K!", GRASS),
        blank(DARK), mole_tile(), blank(DARK),
        text_tile("press", DARK, size=26), text_tile("a key", DARK, size=26), text_tile("◀ ▶\nquit", DARK, (170, 170, 170), 22),
    ]
    if best:
        tiles[3] = text_tile("best", DARK, size=26)
        tiles[5] = text_tile(str(best), DARK, (255, 210, 0), 30)
    show_tiles(pad, tiles)
    wait_for_press(pad)


def game_over(pad, game, best):
    red, gold = (150, 20, 20), (255, 210, 0)
    average = round(1000 * game.reaction_total / game.hits) if game.hits else 0
    tiles = [
        text_tile("GA", red), text_tile("ME", red), text_tile("!", red),
        text_tile("score", DARK, size=24), text_tile(str(game.score), DARK, gold, 30),
        text_tile(f"level\n{game.level}", DARK, size=24),
        text_tile(f"avg\n{average}ms", DARK, size=22), text_tile(f"best\n{max(game.score, best)}", DARK, size=22),
        text_tile(f"streak\n{game.best_streak}", DARK, size=22),
    ]
    if game.score > best:
        tiles[5] = text_tile("NEW\nBEST", gold, "black", 26)
    show_tiles(pad, tiles)
    print(f"  {game.hits} hits, average reaction {average} ms, longest streak {game.best_streak}, level {game.level}")
    time.sleep(1.5)
    pad.draw(text_tile("play\nagain", GRASS, size=24), 8)
    wait_for_press(pad)


def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    pad = Pad()
    try:
        best = load_best()
        print(__doc__)
        while True:
            title_screen(pad, best)
            game = Game(pad, best)
            score = game.run()
            if score > best:
                print(f"  New best: {score}!")
            game_over(pad, game, best)
            if score > best:
                best = score
                save_best(best)
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        pad.clear()
        pad.close()


if __name__ == "__main__":
    main()
