"""
render_reel.py — make a vertical (9:16) promo reel for one Sobha project.

Gemini writes a spoken spotlight -> Kokoro voices it -> ffmpeg builds a mobile
1080x1920 reel:
  * full-screen property photos (no shake), smooth crossfade/slide transitions
  * a waist-up keyed avatar (green removed) in the bottom-right corner
  * the project title + starting price as a lower-third that fades in

Everything is local/free (Kokoro TTS + the ffmpeg bundled with imageio-ffmpeg);
only the narration uses Gemini (GEMINI_API_KEY).

Usage (from the repo root):
    python tools/render_reel.py                 # project #1 (The Grove)
    python tools/render_reel.py 19              # by order number (1..N)
    python tools/render_reel.py "sobha orbis"   # by name / slug substring
    python tools/render_reel.py 1 --regen       # force new narration/voice

Output: reels/bella_<slug>_reel.mp4
"""
import asyncio
import os
import subprocess
import sys
import wave
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from brain import Brain            # noqa: E402
from voice import KokoroTTS        # noqa: E402
from featured import Featured      # noqa: E402
import kb                          # noqa: E402
import imageio_ffmpeg              # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
W, H = 1080, 1920
FPS = 25
SR = 24000
D = 0.8                             # crossfade duration between photos
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
TRANS = ["fade", "slideleft", "fade", "slideup",
         "fade", "slideright", "fade", "slidedown", "fade"]

# Some project folders mix in floor-plan schematics whose filenames don't mark
# them. Curate the photogenic shots per folder-slug; anything not listed here
# falls back to "all images in the folder".
CURATED = {
    "the_grove": ["the_grove_01_gallery.webp", "the_grove_11_image.webp",
                  "the_grove_02_gallery.webp", "the_grove_04_gallery.webp",
                  "the_grove_05_image.webp", "the_grove_06_image.webp",
                  "the_grove_07_image.webp", "the_grove_30_floorplan.webp",
                  "the_grove_31_floorplan.webp", "the_grove_09_image.webp"],
}


def run_ff(argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        print("\n[ffmpeg ERROR]\n" + "\n".join(r.stderr.strip().splitlines()[-15:]))
        raise SystemExit(1)


def pick_project(featured, selector):
    if selector is None:
        return featured.projects[0]
    if selector.isdigit():
        n = int(selector)
        for p in featured.projects:
            if p.order == n:
                return p
        raise SystemExit(f"No project with order {n} (have 1..{len(featured.projects)}).")
    s = selector.lower()
    for p in featured.projects:                 # name / alias / folder substring
        if s in p.name.lower() or any(s in a for a in p.aliases) \
           or s in Path(p.media_dir).name:
            return p
    raise SystemExit(f"No project matching '{selector}'.")


async def build(selector, regen):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    persona = yaml.safe_load((ROOT / "persona.yaml").read_text())
    knowledge, _ = kb.load(ROOT, cfg["stream"]["theme"])
    m = cfg.get("media") or {}
    featured = Featured((cfg.get("data") or {}).get("sobha_featured"),
                        m.get("projects_root"))
    if not featured.projects:
        raise SystemExit("No featured projects with media are configured.")

    proj = pick_project(featured, selector)
    slug = Path(proj.media_dir).name[3:] or "project"
    curated = CURATED.get(slug)
    photos = ([str(Path(proj.media_dir) / n) for n in curated
               if (Path(proj.media_dir) / n).exists()] if curated else list(proj.images))
    if not photos:
        raise SystemExit(f"{proj.name}: no images found in {proj.media_dir}")
    price = next((l.split(":", 1)[1].split("|")[0].replace("*", "").strip()
                  for l in proj.facts.splitlines() if l.startswith("Starting price")), "")
    print(f"[reel] {proj.name}  (#{proj.order}, {len(photos)} photos, price {price or 'n/a'})")

    outdir = ROOT / "reels"
    outdir.mkdir(exist_ok=True)
    cache = outdir / ".cache"
    cache.mkdir(exist_ok=True)
    wav_path = cache / f"{slug}.wav"

    if wav_path.exists() and not regen:
        print("[reel] reusing cached voice (pass --regen for a fresh take)")
    else:
        brain = Brain(cfg, persona, knowledge)
        sentences = [s async for s in brain.narrate_project(proj.name, proj.facts, [])]
        print(f"[reel] narration: {' '.join(sentences)}")
        tts = KokoroTTS(cfg)
        pcm = bytearray()
        silence = b"\x00\x00" * int(SR * 0.18)
        for s in sentences:
            async for chunk in tts.synth(s):
                pcm += chunk
            pcm += silence
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(bytes(pcm))
    with wave.open(str(wav_path), "rb") as w:
        dur = w.getnframes() / w.getframerate()

    n = len(photos)
    slide = (dur + (n - 1) * D) / n            # so the xfade chain == voice length
    print(f"[reel] voice {dur:.1f}s | {n} photos x {slide:.2f}s")

    # ---- pass 1: full-screen static photos + smooth transitions + title ----
    inputs = []
    for p in photos:
        inputs += ["-loop", "1", "-r", str(FPS), "-i", p]
    parts = []
    for i in range(n):
        # STATIC full-screen cover image (no zoom -> zero shake); animation is
        # entirely in the xfade transitions. Normalise fps/timebase for xfade.
        parts.append(
            f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},trim=duration={slide:.3f},setpts=PTS-STARTPTS,"
            f"fps={FPS},settb=AVTB,setsar=1,format=yuv420p[v{i}]"
        )
    prev = "[v0]"
    for i in range(1, n):
        off = i * (slide - D)
        parts.append(f"{prev}[v{i}]xfade=transition={TRANS[(i - 1) % len(TRANS)]}:"
                     f"duration={D}:offset={off:.3f}[x{i}]")
        prev = f"[x{i}]"

    if os.path.exists(FONT) and price:
        fade = "if(lt(t,1.0),0,if(lt(t,1.7),(t-1.0)/0.7,1))"
        title = (f"drawtext=fontfile='{FONT}':text='{proj.name}':fontsize=62:"
                 f"fontcolor=white:borderw=5:bordercolor=black@0.85:"
                 f"x=56:y=H-360:alpha='{fade}'")
        pr = (f"drawtext=fontfile='{FONT}':text='from {price}':fontsize=44:"
              f"fontcolor=0xF5D28C:borderw=4:bordercolor=black@0.85:"
              f"x=58:y=H-286:alpha='{fade}'")
        parts.append(f"{prev}{title},{pr}[v]")
    else:
        parts.append(f"{prev}null[v]")

    tmpdir = cache / slug
    tmpdir.mkdir(exist_ok=True)
    slideshow = tmpdir / "slideshow.mp4"
    run_ff([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(parts),
            "-map", "[v]", "-t", f"{dur:.3f}", "-r", str(FPS),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(slideshow)])

    # ---- pass 2: waist-up keyed avatar bottom-right + mux voice ----
    talk = sorted((ROOT / "clips/avatar_talk_loops").glob("*.mp4"))
    if not talk:
        raise SystemExit("No talking-avatar clip in clips/avatar_talk_loops/")
    avatar = talk[0]
    # crop to a waist-up bust (top ~79% of the frame -> head..just below belt),
    # relative to the clip's own height so it works if the clip changes; this
    # also removes the Gemini watermark baked into the clip's lower corner.
    fc2 = ("[1:v]crop=in_w:'ih*0.79':0:'ih*0.055',"
           "colorkey=0x4d8a4e:0.18:0.04,scale=300:-1,setsar=1[av];"
           "[0:v][av]overlay=W-w-26:H-h:shortest=0[v]")
    out = ROOT / "reels" / f"bella_{slug}_reel.mp4"
    run_ff([FFMPEG, "-y", "-i", str(slideshow),
            "-stream_loop", "-1", "-i", str(avatar), "-i", str(wav_path),
            "-filter_complex", fc2, "-map", "[v]", "-map", "2:a",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", str(out)])
    print(f"[reel] DONE -> {out.relative_to(ROOT)}")


def main():
    args = [a for a in sys.argv[1:]]
    regen = "--regen" in args
    args = [a for a in args if a != "--regen"]
    selector = args[0] if args else None
    asyncio.run(build(selector, regen))


if __name__ == "__main__":
    main()
