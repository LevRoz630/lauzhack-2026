"""Screen overlay: covers the screen with "Away" while the camera sees nobody, and with "Private" while
someone else is looking at it; also shows a small toast when the hub has a phone nudge.

Run:  /usr/bin/python3 -m hub.overlay            (add --no-lock to only cover the screen)
The cover hides the screen, it is not a security boundary; the real lock is the desktop's own.
Esc or a click lifts the cover for a minute, in case the camera is wrong.
"""
import argparse
import asyncio
import json
import subprocess
import threading
import time
import tkinter as tk

import aiohttp

HUB = "http://127.0.0.1:8770/ws"
GRACE = 2.0      # seconds in Away before the screen is covered; Away itself needs 3 s without a face, so 5 s in all
SNOOZE = 60.0
FONT = "Inter"       # needs a Tk with real font support: run this with the system Python
LOCK_AFTER = 2.0     # seconds in Away before the real screen lock (password to return); off with --no-lock

latest = {"state": "unknown", "since": 0.0, "nudge": "", "preview": ""}
PREVIEW_FILES = {"live": "/tmp/breakpoint_page_live.png", "shot": "/tmp/breakpoint_page_shot.png"}


def listen() -> None:
    async def run() -> None:
        while True:
            try:
                async with aiohttp.ClientSession() as http, http.ws_connect(HUB) as ws:
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            continue
                        d = json.loads(m.data)
                        if d.get("type") == "sessions":
                            u = d.get("user", {})
                            latest.update(state=u.get("state", "unknown"), since=u.get("for_s", 0.0), nudge=u.get("nudge", ""),
                                          preview=u.get("page_preview", ""))
            except Exception:
                latest.update(state="unknown", nudge="")
            await asyncio.sleep(1)
    asyncio.run(run())


def main(lock: bool = True) -> None:
    threading.Thread(target=listen, daemon=True).start()
    root = tk.Tk()
    root.configure(bg="black")
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.withdraw()
    word = tk.Label(root, text="Away", fg="#8b949e", bg="black", font=(FONT, 72, "bold"))
    word.pack(expand=True)
    toast = tk.Toplevel(root)
    toast.overrideredirect(True)      # borderless, and the window manager does not give it focus
    toast.attributes("-topmost", True)
    toast.configure(bg="#58a6ff")     # shows as a thin border around the card
    card = tk.Frame(toast, bg="#0d1117")
    card.pack(padx=2, pady=2, fill="both", expand=True)
    toast.withdraw()

    def fill_card(text: str) -> None:
        """A greeting in large type; under it one row per agent: its name, then what happened."""
        for child in card.winfo_children():
            child.destroy()
        lines = text.split("\n")
        rows = [line for line in lines if ": " in line and len(lines) > 1]
        heads = [line for line in lines if line not in rows]
        width = 620 if not rows else 1080
        for i, line in enumerate(heads):
            tk.Label(card, text=line, fg="#f0f6fc", bg="#0d1117", font=(FONT, 24, "bold"), anchor="w", justify="left",
                     wraplength=width - 60).grid(row=i, column=0, columnspan=2, sticky="w", padx=28, pady=(20, 14 if rows else 20))
        for j, line in enumerate(rows):
            name, rest = line.split(": ", 1)
            last = j == len(rows) - 1
            tk.Label(card, text=name, fg="#58a6ff", bg="#0d1117", font=(FONT, 17, "bold"), anchor="nw",
                     justify="left", wraplength=230).grid(row=len(heads) + j, column=0, sticky="nw", padx=(28, 18), pady=(6, 22 if last else 6))
            tk.Label(card, text=rest, fg="#e6edf3", bg="#0d1117", font=(FONT, 17), anchor="nw", justify="left",
                     wraplength=width - 330).grid(row=len(heads) + j, column=1, sticky="nw", padx=(0, 28), pady=(6, 22 if last else 6))
        toast.update_idletasks()
        w, h = max(width, card.winfo_reqwidth() + 4), card.winfo_reqheight() + 4
        # a summary sits at the bottom, clear of the page preview in the top right corner
        top = 28 if not rows else root.winfo_screenheight() - h - 70
        toast.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{top}")

    toast_text = {"v": ""}
    # the page key: what the camera sees during the countdown, then the photo it took, top right
    preview = tk.Toplevel(root)
    preview.overrideredirect(True)
    preview.attributes("-topmost", True)
    preview.configure(bg="#58a6ff")
    picture = tk.Label(preview, bd=0)
    picture.pack(padx=3, pady=3)
    preview.geometry(f"+{root.winfo_screenwidth() - 510}+24")
    preview.withdraw()
    shown_preview = {"mode": "", "image": None}
    state = {"shown": False, "snooze_until": 0.0, "locked": False}

    def snooze(_event=None) -> None:
        if latest["state"] != "shoulder":      # "Private" cannot be dismissed from the screen by whoever is looking
            state["snooze_until"] = time.time() + SNOOZE

    root.bind("<Escape>", snooze)
    root.bind("<Button-1>", snooze)

    def tick() -> None:
        if latest["state"] == "shoulder":
            cover = "Private"
        elif latest["state"] == "away" and latest["since"] >= GRACE:
            cover = "Away"
        else:
            cover = ""
        want = bool(cover) and time.time() > state["snooze_until"]
        if want:
            word.config(text=cover, fg="#8b949e" if cover == "Away" else "#e6edf3")
        if want and not state["shown"]:
            root.deiconify()
            root.attributes("-fullscreen", True)
            root.lift()
        elif not want and state["shown"]:
            root.withdraw()
        state["shown"] = want
        if lock and latest["state"] == "away" and latest["since"] >= LOCK_AFTER and not state["locked"]:
            subprocess.Popen(["cinnamon-screensaver-command", "--lock"])
            state["locked"] = True
        elif latest["state"] != "away":
            state["locked"] = False
        nudge = latest["nudge"] if not want else ""
        if nudge != toast_text["v"]:
            if nudge:
                fill_card(nudge)
                toast.deiconify()
                toast.lift()
            else:
                toast.withdraw()
            toast_text["v"] = nudge
        mode = latest["preview"]
        if mode:
            try:
                image = tk.PhotoImage(file=PREVIEW_FILES[mode])
                picture.config(image=image)
                shown_preview["image"] = image          # keep a reference or Tk drops the picture
                preview.configure(bg="#58a6ff" if mode == "live" else "#3fb950")
                if not shown_preview["mode"]:
                    preview.deiconify()
                    preview.lift()
            except tk.TclError:
                pass                                    # the file is being rewritten; the next tick has it
        elif shown_preview["mode"]:
            preview.withdraw()
        shown_preview["mode"] = mode
        root.after(150, tick)

    tick()
    root.mainloop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-lock", action="store_true", help="only cover the screen, do not lock the session")
    main(not ap.parse_args().no_lock)
