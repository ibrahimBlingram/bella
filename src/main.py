"""
main.py — the full live orchestrator (Phase 2b).

  join      -> greet by name (template, instant, no LLM cost)
  comment   -> human-jitter delay -> Gemini answer (English; Arabic if the comment
               is Arabic AND the Arabic voice is enabled) -> speak
  idle      -> narrate next no-repeat topic (English) -> speak
  long idle -> swap background to a full-screen app demo

A speak-lock means Bella never talks over herself. While she speaks the OBS
avatar shows the TALK loop; when silent, the IDLE loop. Nothing here crashes the
stream — the brain retries transient errors and the listener auto-reconnects.

Run from the project root, WITH THE ACCOUNT LIVE:   python src/main.py
"""
import asyncio
import os
import random
import re
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from brain import Brain, is_arabic
from listener import Listener
from obs_control import OBS
from topics import TopicQueue
from voice import Voice
import kb

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent


async def _one(s: str):
    yield s


def _seeds(theme_text: str):
    out = []
    for line in theme_text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            out.append(line[2:].strip())
        else:
            m = re.match(r"\[\d+\]\s+(.*)", line)
            if m:
                out.append(m.group(1).strip())
    return out


async def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    persona = yaml.safe_load((ROOT / "persona.yaml").read_text())
    theme = cfg["stream"]["theme"]
    knowledge, theme_text = kb.load(ROOT, theme)
    seeds = _seeds(theme_text) or [f"why {theme} matters for creators"]

    brain = Brain(cfg, persona, knowledge)
    voice = Voice(cfg)
    obs = OBS(cfg)
    topics = TopicQueue(seeds)
    events: asyncio.Queue = asyncio.Queue()
    username = os.environ[cfg["stream"]["username_env"]]
    listener = Listener(username, events)

    s = cfg["stream"]
    jitter = s["response_jitter"]
    idle_after = s["idle_seconds"]
    demo_after = s["demo_after_idle"]
    qa_cd = s["qa_cooldown"]

    speak_lock = asyncio.Lock()
    state = {"last_activity": time.time(), "last_qa": 0.0, "demo_on": False}
    obs.set_talking(False)

    async def speak(sentences, lang="en"):
        async with speak_lock:
            await voice.say(
                sentences, lang=lang,
                on_start=lambda: obs.set_talking(True),
                on_stop=lambda: obs.set_talking(False),
            )

    async def handle_events():
        while True:
            kind, name, text = await events.get()
            state["last_activity"] = time.time()
            state["demo_on"] = False               # someone's here -> leave demo mode
            if kind == "join":
                greet = random.choice(persona["greetings"]).format(name=name)
                await speak(_one(greet))
            elif kind == "comment":
                if time.time() - state["last_qa"] < qa_cd:
                    continue                        # cooldown: don't answer everything
                await asyncio.sleep(random.uniform(*jitter))   # human-like delay
                state["last_qa"] = time.time()
                lang = "ar" if (is_arabic(text) and voice.has_arabic) else "en"
                await speak(brain.answer(text, lang), lang=lang)

    async def idle_engine():
        while True:
            await asyncio.sleep(2)
            if voice.speaking.is_set():
                continue
            quiet = time.time() - state["last_activity"]
            if quiet >= demo_after and not state["demo_on"]:
                obs.demo_background()               # full-screen app demo
                state["demo_on"] = True
            if quiet >= idle_after:
                topic = topics.next()
                await speak(brain.narrate(topic, topics.covered))
                topics.mark(topic)
                state["last_activity"] = time.time()

    print(f"Bella LIVE | theme={theme} | listening to {username}")
    await asyncio.gather(listener.run(), handle_events(), idle_engine())


if __name__ == "__main__":
    asyncio.run(main())
