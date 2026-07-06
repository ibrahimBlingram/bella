"""
test_obs.py — Phase 2a: see Bello in OBS, no TikTok yet.

Bello hosts the theme on a loop (idle narration). As he speaks, OBS switches
the avatar from the IDLE loop to the TALK loop and back, and his audio goes to
the virtual audio device OBS is capturing. This is the full OBS experience minus
the live chat — proof that visuals + audio are wired correctly.

Prereqs:
  - OBS open, scene "Live" with AvatarIdle / AvatarTalk / Background sources
  - OBS WebSocket enabled; password matches config.yaml obs.password
  - config.yaml tts.output_device set to your virtual cable (BlackHole / VB-CABLE)
  - pip install obsws-python

Run:  python test_obs.py     (Ctrl-C to stop)
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
from obs_control import OBS      # noqa: E402
import kb                        # noqa: E402

cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
persona = yaml.safe_load((ROOT / "persona.yaml").read_text())
theme = cfg["stream"]["theme"]
knowledge, theme_text = kb.load(ROOT, theme)

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
    obs = OBS(cfg)                 # connects to OBS WebSocket
    topics = TopicQueue(seeds)

    obs.set_talking(False)         # start on the idle loop
    print(f"OBS live sim  |  theme: {theme}  |  Ctrl-C to stop")

    while True:
        topic = topics.next()
        print(f"\n[topic] {topic}")

        async def seg():
            async for s in brain.narrate(topic, topics.covered):
                print(f"  [bello] {s}")
                yield s

        # on_start -> switch to TALK loop; on_stop -> back to IDLE loop
        await voice.say(
            seg(),
            lang="en",
            on_start=lambda: obs.set_talking(True),
            on_stop=lambda: obs.set_talking(False),
        )
        topics.mark(topic)
        await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
