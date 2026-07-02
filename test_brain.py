"""
STAGE 1 — Brain only. Tests: GEMINI_API_KEY + RAG + streaming.
No audio, no OBS, no TikTok. Run: python test_brain.py
"""
import asyncio
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv()

from brain import Brain  # noqa: E402
import kb                # noqa: E402

cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
persona = yaml.safe_load((ROOT / "persona.yaml").read_text())
knowledge, _ = kb.load(ROOT, cfg["stream"]["theme"])


async def main():
    brain = Brain(cfg, persona, knowledge)
    questions = [
        "is blingram free to use?",
        "how do creators actually earn on blingram?",
        "are you a real person?",
    ]
    for q in questions:
        print(f"\n[viewer] {q}\n[bella]  ", end="", flush=True)
        async for sentence in brain.answer(q):
            print(sentence, end=" ", flush=True)
        print()
    print("\n\n[OK] Brain works.")


asyncio.run(main())
