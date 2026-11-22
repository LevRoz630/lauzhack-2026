#!/usr/bin/env python3
"""Teach the desk view to spot a phone in your hand, then check it.

Usage:  uv run python scripts/train_phone.py            add three takes
        uv run python scripts/train_phone.py --clear    forget everything first

The hub and sense must be running. It records the phone, then the same desk without the phone,
in one sitting, so the two sets differ only by the phone.
"""
import argparse
import asyncio
import json

import aiohttp

HUB = "http://127.0.0.1:8770"
TAKES = 3


async def desk(http: aiohttp.ClientSession) -> dict:
    return (await (await http.get(HUB + "/state")).json())["user"].get("desk") or {}


async def countdown(text: str, seconds: int = 3) -> None:
    for n in range(seconds, 0, -1):
        print(f"\r{text} in {n} ", end="", flush=True)
        await asyncio.sleep(1)
    print("\r" + " " * (len(text) + 8) + "\r", end="")


async def share_phone(http: aiohttp.ClientSession, seconds: float) -> float:
    """Fraction of the next `seconds` in which the desk view says phone."""
    hits = total = 0
    for _ in range(int(seconds * 2)):
        await asyncio.sleep(0.5)
        total += 1
        hits += (await desk(http)).get("desk") == "phone"
    return hits / total


async def main(clear: bool) -> None:
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(HUB + "/ws")
        if clear:
            await ws.send_str(json.dumps({"type": "teach", "label": "clear"}))
            await asyncio.sleep(1)
            print("Cleared.")
        d = await desk(http)
        if "examples" not in d:
            raise SystemExit("No desk view yet. Is sense running and the camera plugged in?")

        for take in range(1, TAKES + 1):
            input(f"Take {take} of {TAKES}: hold your phone the way you normally would, a bit differently each take. Press Enter.")
            await countdown("Recording")
            await ws.send_str(json.dumps({"type": "teach", "label": "phone"}))
            await asyncio.sleep(1)
            while (await desk(http)).get("teaching"):
                print(f"\rRecording, keep holding the phone: {(await desk(http)).get('teach_left', 0)} s ", end="", flush=True)
                await asyncio.sleep(0.5)
            print("\rSaved." + " " * 40)

        # No-phone examples from this same sitting, so the two sets differ only by the phone.
        await ws.send_str(json.dumps({"type": "teach", "label": "fresh"}))
        for take, what in enumerate(("type on the laptop", "rest your hands, lean back or hold a cup"), 1):
            input(f"No-phone take {take} of 2: put the phone out of sight and {what}. Press Enter.")
            await countdown("Recording")
            await ws.send_str(json.dumps({"type": "teach", "label": "other"}))
            await asyncio.sleep(1)
            while (await desk(http)).get("teaching"):
                print(f"\rRecording, no phone: {(await desk(http)).get('teach_left', 0)} s ", end="", flush=True)
                await asyncio.sleep(0.5)
            print("\rSaved." + " " * 40)

        d = await desk(http)
        print(f"Examples: phone {d['examples'].get('phone', 0)}, no phone {d['examples'].get('other', 0)}")

        input("Check 1: hold the phone in a NEW position. Press Enter.")
        await countdown("Checking")
        with_phone = await share_phone(http, 8)
        input("Check 2: put the phone away and type or rest your hands. Press Enter.")
        await countdown("Checking")
        without = await share_phone(http, 8)
        print(f"With the phone it said phone {with_phone:.0%} of the time (want high).")
        print(f"Without the phone it said phone {without:.0%} of the time (want 0%).")
        if with_phone < 0.7 or without > 0.1:
            print("Not reliable yet: run this script again to add three more takes.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true", help="forget all examples first")
    asyncio.run(main(ap.parse_args().clear))
