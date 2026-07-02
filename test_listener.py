"""
STAGE 4 — Listener only. Tests: TikTokLive + EulerStream.
Point it at ANY account that is currently LIVE (doesn't have to be yours).
Prints join/comment events. No audio, no OBS. Run: python test_listener.py

If TikTokLive errors on signing, add your free EulerStream API key per their docs.
"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv()

from listener import Listener  # noqa: E402

TARGET = "REPLACE_WITH_A_CURRENTLY_LIVE_USERNAME"  # no leading @


async def main():
    q = asyncio.Queue()
    listener = Listener(TARGET, q)
    asyncio.create_task(listener.run())
    print(f"Listening to @{TARGET} ... Ctrl+C to stop.")
    while True:
        kind, name, text = await q.get()
        print(f"[{kind:7}] {name}: {text}")


asyncio.run(main())
