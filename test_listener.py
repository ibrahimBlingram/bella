"""
STAGE 4 — Listener only. Tests: TikTokLive + EulerStream.
Prints join/comment events. No audio, no OBS.

By default it listens to TIKTOK_USERNAME from .env — the same account main.py
uses. That account must be LIVE RIGHT NOW, or TikTok just reports it offline and
the listener retries forever (which is correct behaviour, not a bug).

To watch someone else's live instead, pass a handle:

    python test_listener.py                    # TIKTOK_USERNAME from .env
    python test_listener.py someoneelse        # any account that is live now
"""
import asyncio
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv()

from listener import Listener  # noqa: E402

# CLI arg wins; otherwise the same env var main.py reads. Never a placeholder —
# a hardcoded one used to send this test at a username that does not exist.
TARGET = (sys.argv[1] if len(sys.argv) > 1
          else os.environ.get("TIKTOK_USERNAME", "")).lstrip("@")

if not TARGET:
    sys.exit("No target. Set TIKTOK_USERNAME in .env, or: python test_listener.py <handle>")


async def main():
    q = asyncio.Queue()
    listener = Listener(TARGET, q)          # <- TikTokLive installs its own SIGINT
                                            #    handler while building the client
    # Take SIGINT back. TikTokLive registers a handler that swallows Ctrl-C, and
    # Listener.run() retries forever by design (the live stream must survive the
    # account going off and back on air) — so between them, Ctrl-C did nothing and
    # the only way out was `pkill` from a second terminal. Registering AFTER the
    # client is constructed means ours wins.
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:         # not supported on some platforms
            signal.signal(sig, lambda *_: stop.set())

    task = asyncio.create_task(listener.run())
    print(f"Listening to @{TARGET} ... Ctrl+C to stop.")
    print("(If it says 'offline', that account simply isn't live right now.)")

    async def drain():
        while True:
            kind, name, text = await q.get()
            print(f"[{kind:7}] {name}: {text}")

    drain_task = asyncio.create_task(drain())
    await stop.wait()                       # <- Ctrl-C lands here
    print("\nstopping...")
    for t in (task, drain_task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    print("stopped.")


asyncio.run(main())
