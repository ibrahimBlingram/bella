# Bello — TikTok-Live AI Streaming Host

Bello is an autonomous live-streaming host for TikTok. He reads the live chat,
answers viewers in a consistent persona, narrates topics when chat is quiet, and
drives an on-screen avatar in OBS that switches between an **idle** loop and a
**talking** loop as she speaks.

- **Brain** — Gemini (`google-genai`) with a small RAG knowledge base, streaming answers sentence-by-sentence.
- **Voice** — pluggable TTS via `tts.provider`:
  - [Chatterbox](https://github.com/resemble-ai/chatterbox) (Resemble AI, MIT, **GPU/CUDA**) — the Vast.ai default. `chatterbox_multi` (Multilingual V3) does Arabic **and** English from one model with zero-shot voice cloning; `chatterbox` (Turbo) is English-only with inline paralinguistic tags (`[laugh]`, `[cough]`, `[sigh]`, `[chuckle]`).
  - [Kokoro](https://github.com/hexgrad/kokoro) local English TTS (free, no key, CPU/Mac) with [edge-tts](https://github.com/rany2/edge-tts) for Arabic — the original local-Mac path. Optional paid ElevenLabs.
- **Avatar / video** — OBS driven over WebSocket (`obsws-python`); instant idle↔talk switching by toggling source visibility.
- **Live chat** — `TikTokLive` listener with auto-reconnect.
- **Audio routing** — TTS is sent to a virtual audio device that OBS captures (BlackHole on macOS, a PulseAudio null sink on Linux/Vast.ai).

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in your keys
```

### Environment keys (`.env`)

| Key | Needed for |
| --- | --- |
| `GEMINI_API_KEY` | Brain (free from [aistudio.google.com](https://aistudio.google.com)) |
| `ELEVENLABS_API_KEY` | Only if you switch Arabic to the paid ElevenLabs engine (optional; Arabic is free via edge-tts by default) |
| `CARTESIA_API_KEY` | Alternative TTS (optional) |
| `TIKTOK_USERNAME` | The handle to go live as (full run only) |
| `EULERSTREAM_API_KEY` | Only if TikTokLive asks for one (optional) |

Configuration (model, voice, theme, OBS sources, idle timings) lives in
[`config.yaml`](config.yaml); the persona and narration prompt in
[`persona.yaml`](persona.yaml); the knowledge base in [`knowledge/`](knowledge/).

## External requirements for a full live run

These are **not** pip-installable and are only needed for the OBS/live stages:

- **BlackHole 2ch** virtual audio driver (so OBS can capture Bello's voice).
- **OBS** running with WebSocket enabled on `:4455`, and a scene named `Live`
  containing sources `AvatarIdle`, `AvatarTalk`, `Background`.
- The TikTok account actually **live** (for `test_listener.py` / `main.py`).

## Running — staged smoke tests

Each stage adds one capability, so you can verify pieces in isolation. Run with
`python <file>` from the repo root.

| Stage | File | Tests | Needs |
| --- | --- | --- | --- |
| 1 | `test_brain.py` | Gemini + RAG + streaming | `GEMINI_API_KEY` |
| 2 | `test_voice.py` | TTS + audio playback | TTS engine |
| 3 | `test_chat.py` | Brain → Voice, language routing | keys above |
| 4 | `test_listener.py` | TikTokLive + EulerStream | account live |
| 2a | `test_obs.py` | Avatar idle↔talk in OBS (no TikTok) | OBS running |
| — | `test_live.py` | Full loop, **no** OBS and **no** TikTok | keys above |

`test_brain.py` is the quickest check — it needs no audio, OBS, or TikTok.

## Full live run

```bash
python src/main.py
```

The orchestrator: greets joiners by name (template, no LLM cost), answers
comments after a human-like delay (Arabic if the comment is Arabic and the
Arabic voice is enabled), narrates no-repeat topics when idle, and swaps to a
full-screen app demo after a long idle. A speak-lock keeps Bello from talking
over himself; the brain retries transient errors and the listener auto-reconnects.

## Vast.ai GPU deployment (headless Linux, 24/7)

Runs the whole stack on a Vast.ai GPU instance (tested target: RTX 3060, 12 GB
VRAM, Ubuntu) with **Chatterbox** TTS on CUDA, **headless OBS** on a virtual
display, and OBS streaming **directly to TikTok over RTMP** (no LIVE Studio).

Chatterbox is GPU-only — it will not run on CPU or MPS. On Vast, keep
`output_device: null`; the PulseAudio null sink handles routing to OBS.

**1. Clone + assets.** Put the repo at `/root/bella` (paths in `config.yaml` are
absolute `/root/bella/...`). Add your assets:

- `voice_samples/bella_ref_en.wav`, `voice_samples/bella_ref_ar.wav` — 5-10s
  voice-clone references (see `voice_samples/README.md`; ref language must match).
- `clips/avatar_idle_loops/idle.mp4`, `clips/avatar_talk_loops/talk.mp4` —
  green-screen avatar loops (chroma-keyed automatically).
- `clips/backgrounds/background.jpg` — background image.

**2. One-time setup** (installs ffmpeg, Xvfb, OBS, PulseAudio + all Python deps,
starts the virtual display `:99` and the `bella_audio` null sink):

```bash
bash scripts/setup_vast.sh
```

**3. Configure `.env`** — copy `.env.example` and fill `GEMINI_API_KEY`,
`TIKTOK_USERNAME`, and for direct RTMP streaming `TIKTOK_SERVER_URL` +
`TIKTOK_STREAM_KEY` (from TikTok LIVE → "stream via third-party software").

**4. Start OBS headlessly and build the scene** (creates the `Live` scene,
looping avatar media sources, image background, the PulseAudio audio capture,
green chroma-key filters, and the Custom/RTMP TikTok stream settings):

```bash
bash scripts/start_obs_headless.sh
DISPLAY=:99 python scripts/setup_obs_scene.py
```

**5. Go live:**

```bash
python src/main.py
```

Start/stop the RTMP stream from `setup_obs_scene.py`'s client (`c.start_stream()`)
or leave OBS streaming continuously for the 24/7 run.

### TTS provider switch

`config.yaml` → `tts.provider`:

| provider | languages | device | notes |
| --- | --- | --- | --- |
| `chatterbox_multi` | Arabic + English | CUDA | Vast.ai default; one model, per-language clone refs |
| `chatterbox` | English | CUDA | Turbo; supports `[laugh]`/`[cough]`/`[sigh]`/`[chuckle]` |
| `kokoro` | English (+ edge/elevenlabs Arabic) | CPU/MPS/CUDA | local Mac path; no GPU required |

## Repository layout

```text
src/            core modules (brain, voice, obs_control, listener, topics, main)
scripts/        Vast.ai deployment (setup_vast.sh, start_obs_headless.sh, setup_obs_scene.py)
knowledge/      RAG knowledge base (product + monthly theme files)
clips/          avatar idle/talk loops + demo backgrounds
voice_samples/  Chatterbox voice-clone reference audio
config.yaml     model, voice, theme, OBS sources, timings
persona.yaml    Bello's persona + narration prompt
test_*.py       staged smoke tests (see table above)
```
