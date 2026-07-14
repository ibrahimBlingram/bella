"""
main.py — the full live orchestrator (Phase 2b).

  join      -> greet by name (template, instant, no LLM cost)
  comment   -> human-jitter delay -> Gemini answer (English; Arabic if the comment
               is Arabic AND the Arabic voice is enabled) -> speak
  idle      -> narrate next no-repeat topic (English) -> speak
  long idle -> swap background to a full-screen app demo

A speak-lock means Bello never talks over himself. While she speaks the OBS
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
from featured import Featured
from listener import Listener
from obs_control import OBS
from paths import abspath
from topics import TopicQueue
from voice import Voice
import kb

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent


async def _one(s: str):
    yield s


def _price_of(proj) -> str:
    for line in proj.facts.splitlines():
        if line.startswith("Starting price"):
            return line.split(":", 1)[1].split("|")[0].replace("*", "").strip()
    return ""


class Slideshow:
    """Rotates a project's media in the OBS background while Bello talks about
    it, and owns the title card that names it.

    The title lives HERE, with the background, on purpose. They used to be set
    from two different places, and a cancelled cycle task could still write one
    more background frame after the next project had already started — so viewers
    saw "The Brooks — from AED 4.16 M" printed over photos of The Pinnacle. A
    title over the wrong price is worse than no title. Owning both means they
    cannot drift apart: every background write goes through this class, and a
    generation counter makes a stale task's write a no-op.
    """

    def __init__(self, obs, seconds: float):
        self.obs = obs
        self.seconds = seconds
        self.task: asyncio.Task | None = None
        self.current: str | None = None     # media_dir of what's showing
        self.gen = 0                        # bumped on every start/stop

    async def _cycle(self, media: list[str], gen: int):
        i = 1
        while True:
            await asyncio.sleep(self.seconds)
            if gen != self.gen:             # superseded — do not touch OBS
                return
            self.obs.show_background(media[i % len(media)])
            i += 1

    async def start(self, project, title: str = "", price: str = ""):
        """Show this project's media AND its title, together. No-op if already on."""
        if not project or not project.media or project.media_dir == self.current:
            return
        await self.stop()
        self.gen += 1
        self.current = project.media_dir
        self.obs.show_background(project.media[0])
        if title:
            self.obs.set_title(title, price)
        else:
            self.obs.hide_title()
        if len(project.media) > 1:
            self.task = asyncio.create_task(self._cycle(project.media, self.gen))

    async def stop(self):
        """Cancel the cycle AND WAIT for it. Cancellation is not instant: without
        the await, the dying task could still push one more background."""
        self.gen += 1                       # any in-flight write is now stale
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None


def _segment_stream(featured: Featured, fact_seeds: list[str], project_share=0.7):
    """Infinite stream of segments, weighted.

    This is a MARKETING channel for Sobha, so the projects are the point — but a
    solid hour of nothing but listings is a channel nobody watches. The Dubai
    real-estate hooks are what stop it reading as a catalogue.

    It used to strictly alternate project / fact / project / fact, i.e. a 50-50
    split: half the airtime went to general Dubai chat rather than to Sobha.

    `project_share` is the fraction of SEGMENTS that promote a project (0.7 = 70%
    projects, 30% Dubai). Segments are emitted to keep the running ratio as close
    to that target as possible, so it holds over any window, not just on average
    across a full lap. Projects still go in order 1..N so the tour is coherent.
    """
    bag: list[str] = []

    def next_fact() -> str:
        nonlocal bag
        if not bag:
            bag = fact_seeds[:]
            random.shuffle(bag)
        return bag.pop()

    projects = 0
    facts = 0
    idx = 0
    while True:
        if not featured.projects:
            return
        # Emit whichever segment moves the running ratio TOWARD the target.
        total = projects + facts
        want_project = (not fact_seeds) or total == 0 or (projects / total) < project_share
        if want_project:
            yield ("project", featured.projects[idx % len(featured.projects)])
            idx += 1
            projects += 1
        else:
            yield ("fact", next_fact())
            facts += 1


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

    # Voice first: whether the English engine can PERFORM [laugh] decides whether
    # the brain is even allowed to write it (see Brain.__init__).
    voice = Voice(cfg)
    brain = Brain(cfg, persona, knowledge, performs_tags=voice.performs_tags)
    obs = OBS(cfg)
    topics = TopicQueue(seeds)

    # Optional: make the LIVE scene look like the exported reel (full-screen
    # background, waist-up avatar bust bottom-right, project title/price overlay).
    o = cfg.get("obs") or {}
    if o.get("reel_layout"):
        obs.title_src = o.get("title_source", "BellaTitle")
        # avatar_drop_px pushes the avatar below the bottom edge so the generator
        # watermark burned into the bottom of the clip is cropped off the canvas.
        obs.apply_reel_layout(drop_px=int(o.get("avatar_drop_px", 0)))

    m = cfg.get("media") or {}
    featured = Featured(abspath((cfg.get("data") or {}).get("sobha_featured")),
                        abspath(m.get("projects_root")))
    slideshow = Slideshow(obs, float(m.get("slide_seconds", 4.5)))
    print(f"Featured projects with visuals: {len(featured.projects)}")
    events: asyncio.Queue = asyncio.Queue()
    username = os.environ[cfg["stream"]["username_env"]]
    listener = Listener(username, events)

    s = cfg["stream"]
    # 70% of segments promote a Sobha project, 30% are Dubai real-estate hooks.
    # This is a marketing channel — the projects are the point — but an unbroken
    # hour of listings is a channel nobody watches.
    segments = (_segment_stream(featured, seeds, float(s.get("project_share", 0.7)))
                if featured.projects else None)
    jitter = s["response_jitter"]
    idle_after = s["idle_seconds"]
    demo_after = s["demo_after_idle"]
    qa_cd = s["qa_cooldown"]

    # Don't greet every single joiner. On a busy stream that is all Bello would
    # ever do — each greeting resets the clock, so he never gets to narrate, and
    # viewers hear "hello ... <silence> ... hello". Batch arrivals instead.
    GREET_EVERY = 20.0
    GREET_MAX_NAMES = 3

    speak_lock = asyncio.Lock()
    state = {
        # When Bello last actually SPOKE. Narration keys off this, not off viewer
        # activity — otherwise a trickle of joiners starves narration forever.
        "last_spoke": time.time(),
        "last_qa": 0.0,
        "last_greet": 0.0,
        "pending_greets": [],      # names that arrived since the last greeting
        "demo_on": False,
        # Kept for reference/logging only. NOTHING may act on this to hide the
        # avatar or stop Bello talking — see idle_engine().
        "viewers": None,
    }
    obs.set_talking(False)
    obs.ensure_avatar_visible()   # a previous run must never leave a blank stream
    # OBS keeps whatever was last on screen. After a restart that means the PREVIOUS
    # run's title sits over the PREVIOUS run's background until the first project
    # loads — i.e. a stale name over an unrelated building, live on stream. Clear it;
    # the first slideshow.start() will set the real one.
    obs.hide_title()

    async def speak(sentences, lang="en"):
        async with speak_lock:
            await voice.say(
                sentences, lang=lang,
                on_start=lambda: obs.set_talking(True),
                on_stop=lambda: obs.set_talking(False),
            )
        # Stamped AFTER he finishes, so the idle gap is measured from silence.
        state["last_spoke"] = time.time()

    async def handle_events():
        while True:
            kind, name, text = await events.get()

            if kind == "viewers":                  # just a count; NOT a trigger
                state["viewers"] = name
                continue

            state["demo_on"] = False               # someone's here -> leave demo mode
            if state["viewers"] in (None, 0):
                state["viewers"] = 1               # (next count event corrects it)

            if kind == "join":
                # Queue the name. A greeting is NOT emitted here — greet_engine
                # batches them. Greeting every joiner inline meant a busy stream
                # got nothing but greetings, one at a time, forever.
                state["pending_greets"].append(name)

            elif kind == "comment":
                if time.time() - state["last_qa"] < qa_cd:
                    continue                        # cooldown: don't answer everything
                await asyncio.sleep(random.uniform(*jitter))   # human-like delay
                state["last_qa"] = time.time()
                lang = "ar" if (is_arabic(text) and voice.has_arabic) else "en"
                # If the question is about a featured project, show it on screen.
                asked = featured.match(text)
                if asked:
                    await slideshow.start(asked, asked.name, _price_of(asked))
                await speak(brain.answer(text, lang), lang=lang)

    async def greet_engine():
        """Greet ARRIVALS IN BATCHES. One greeting can welcome several people, and
        never more often than GREET_EVERY — so greetings can't crowd out the
        narration that actually carries the stream."""
        while True:
            await asyncio.sleep(2)
            names = state["pending_greets"]
            if not names or voice.speaking.is_set():
                continue
            if time.time() - state["last_greet"] < GREET_EVERY:
                continue
            batch, state["pending_greets"] = names[:GREET_MAX_NAMES], []
            state["last_greet"] = time.time()
            if len(batch) == 1:
                line = random.choice(persona["greetings"]).format(name=batch[0])
            else:
                line = f"welcome in {', '.join(batch[:-1])} and {batch[-1]}!"
            await speak(_one(line))

    async def idle_engine():
        # "Silent mode" is GONE. It used to hide the avatar and the title and stop
        # Bello talking whenever the room read empty, to save API cost with nobody
        # watching. It is a trap, and it took down a live stream:
        #
        #   You go live. Nobody has joined YET, so the viewer count is 0. After the
        #   grace period Bello hides his own avatar, hides the title, and shuts up —
        #   so the very first person who clicks in finds a dead, blank stream and
        #   leaves. The show turns itself off exactly when it needs to be a show.
        #
        # An empty room is not a reason to stop performing; it is the reason TO
        # perform. Bello now always shows and always talks. Nothing in this file may
        # hide the avatar again.
        while True:
            await asyncio.sleep(2)

            if voice.speaking.is_set():
                continue

            # Narration is timed from when Bello last SPOKE — not from viewer
            # activity. Keying it off activity meant every join reset the clock, so
            # a steady trickle of joiners starved narration completely: viewers got
            # a greeting, then dead air, then another greeting.
            quiet = time.time() - state["last_spoke"]

            if segments is not None:
                # Visual tour: promote a project (its images play behind him),
                # then a Dubai real-estate hook, then the next project, ...
                if quiet < idle_after:
                    continue
                kind, payload = next(segments)
                if kind == "project":
                    # Background AND title set together — they cannot desync.
                    await slideshow.start(payload, payload.name, _price_of(payload))
                    await speak(brain.narrate_project(
                        payload.name, payload.facts, topics.covered))
                    topics.mark(payload.name)
                else:
                    obs.hide_title()                # Dubai hook, not a project
                    await speak(brain.narrate(payload, topics.covered))
                    topics.mark(payload)
                continue

            # Fallback (no media wired): original topic + app-demo behaviour.
            if quiet >= demo_after and not state["demo_on"]:
                obs.demo_background()               # full-screen app demo
                state["demo_on"] = True
            if quiet >= idle_after:
                topic = topics.next()
                await speak(brain.narrate(topic, topics.covered))
                topics.mark(topic)

    async def watchdog():
        """Bello going mute is the one failure nobody notices until viewers do — it
        looks identical to him simply having nothing to say. He once wedged on a
        dead audio device and stayed silent for the rest of the stream without a
        single line in the log. So: if he has not spoken in a long time while the
        room is NOT empty, say so loudly."""
        WARN_AFTER = 60.0
        warned = False
        while True:
            await asyncio.sleep(15)
            if voice.speaking.is_set():
                warned = False
                continue
            # THE AVATAR MUST NEVER BE OFF SCREEN. It went missing on a live stream
            # once and viewers saw a blank feed. Nothing in this code hides it any
            # more, but a stale OBS scene, a crashed run, or a stray click in the
            # OBS GUI still can — so check every 15s and put it back. Cheap, and it
            # makes a blank stream self-healing rather than permanent.
            obs.ensure_avatar_visible()

            gap = time.time() - state["last_spoke"]
            if gap > WARN_AFTER and not warned:
                print(f"[watchdog] Bello has not spoken for {gap:.0f}s. Audio device "
                      f"wedged, or the brain is failing. Check the log above for "
                      f"[voice]/[brain] errors.")
                warned = True
            elif gap <= WARN_AFTER:
                warned = False

    print(f"Bello LIVE | theme={theme} | listening to {username}")
    await asyncio.gather(listener.run(), handle_events(),
                         greet_engine(), idle_engine(), watchdog())


if __name__ == "__main__":
    asyncio.run(main())
