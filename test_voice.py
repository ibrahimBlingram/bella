"""
STAGE 2 — Voice only. Tests: TTS key + audio playback.
Plays to your DEFAULT speakers (not BlackHole yet). Run: python test_voice.py
"""
import asyncio
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv()

from voice import Voice  # noqa: E402

cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
cfg["tts"]["output_device"] = None  # default speakers for testing


async def _one(s):
    yield s


async def main():
    voice = Voice(cfg)
    print("Speaking... you should HEAR Bella now.")
    await voice.say(_one("Hey! If you can hear me, the voice pipeline works."))
    print("[OK] Voice works.")


asyncio.run(main())
