"""
test_live.py — see/hear how the live goes, with NO OBS and NO TikTok.

Bella hosts the current month's THEME on her own: she picks a fresh topic, talks
about it for a couple of sentences, ties it back to Blingram, then moves to the
next topic — on a loop, never repeating. This is exactly what she does whenever
chat is quiet (the "idle narration" half of the stream).

The other half — answering live comments — is what test_chat.py already showed.
main.py (Phase 2) combines both and adds the real TikTok chat + OBS avatar.

Run:  python test_live.py     (Ctrl-C to stop)
"""
import asyncio
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv()

from brain import Brain          # noqa: E402
from voice import Voice          # noqa: E402
from topics import TopicQueue    # noqa: E402
import kb                        # noqa: E402

cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
cfg["tts"]["output_device"] = None
persona = yaml.safe_load((ROOT / "persona.yaml").read_text())
theme = cfg["stream"]["theme"]
knowledge, theme_text = kb.load(ROOT, theme)

# Pull topic seeds from the theme file. Supports both formats:
#   "- some seed"      (the simple theme files)
#   "[01] SOME TITLE"  (the Bella_KB topics.txt files)
seeds = []
for line in theme_text.splitlines():
    line = line.strip()
    if line.startswith("- "):
        seeds.append(line[2:].strip())
    else:
        m = re.match(r"\[\d+\]\s+(.*)", line)
        if m:
            seeds.append(m.group(1).strip())
if not seeds:
    seeds = [f"why {theme} matters for creators", f"a quick {theme} tip"]


async def main():
    brain = Brain(cfg, persona, knowledge)
    voice = Voice(cfg)
    topics = TopicQueue(seeds)
    print(f"LIVE SIM  |  theme: {theme}  |  {len(seeds)} topics  |  Ctrl-C to stop\n")

    while True:
        topic = topics.next()
        print(f"\n[topic] {topic}")

        async def segment():
            async for s in brain.narrate(topic, topics.covered):
                print(f"  [bella] {s}")
                yield s

        await voice.say(segment())
        topics.mark(topic)
        await asyncio.sleep(2)   # short beat between segments


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
