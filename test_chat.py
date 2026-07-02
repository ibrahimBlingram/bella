"""
STAGE 3 — Brain -> Voice end to end, with language routing.
English comments -> English answer (Kokoro). Arabic comments -> Arabic answer
(ElevenLabs Sara), but only if tts.arabic.enabled and ELEVENLABS_API_KEY is set.
Run: python test_chat.py   (blank line to quit)
"""
import asyncio
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv()

from brain import Brain, is_arabic   # noqa: E402
from voice import Voice              # noqa: E402
import kb                            # noqa: E402

cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
cfg["tts"]["output_device"] = None
persona = yaml.safe_load((ROOT / "persona.yaml").read_text())
knowledge, _ = kb.load(ROOT, cfg["stream"]["theme"])


async def main():
    brain = Brain(cfg, persona, knowledge)
    voice = Voice(cfg)
    print(f"Type a question (blank to quit). Arabic voice: "
          f"{'ON' if voice.has_arabic else 'OFF'}")
    while True:
        q = (await asyncio.to_thread(input, "\n[viewer] ")).strip()
        if not q:
            break
        # Arabic only if we actually have an Arabic voice; else stay English.
        lang = "ar" if (is_arabic(q) and voice.has_arabic) else "en"

        async def spoken():
            async for s in brain.answer(q, lang):
                print(f"[bella]  {s}")
                yield s

        await voice.say(spoken(), lang=lang)
    print("\n[OK] Core loop works.")


asyncio.run(main())
