# Bello — TikTok-Live AI Streaming Host

Bello is an autonomous live-streaming host for TikTok. He reads the live chat,
answers viewers in a consistent persona, narrates topics when chat is quiet, and
drives an on-screen avatar in OBS that switches between an **idle** loop and a
**talking** loop as she speaks.

- **Brain** — Gemini (`google-genai`) with a small RAG knowledge base, streaming answers sentence-by-sentence.
- **Voice** — local [Kokoro](https://github.com/hexgrad/kokoro) TTS for English (free, no key) and [edge-tts](https://github.com/rany2/edge-tts) neural voices for Arabic (also free, no key). Optional paid ElevenLabs path.
- **Avatar / video** — OBS driven over WebSocket (`obsws-python`); instant idle↔talk switching by toggling source visibility.
- **Live chat** — `TikTokLive` listener with auto-reconnect.
- **Audio routing** — TTS is sent to a virtual audio device (BlackHole on macOS) that OBS captures.

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

## Repository layout

```
src/            core modules (brain, voice, obs_control, listener, topics, main)
knowledge/      RAG knowledge base (product + monthly theme files)
clips/          avatar idle/talk loops + demo backgrounds
config.yaml     model, voice, theme, OBS sources, timings
persona.yaml    Bello's persona + narration prompt
test_*.py       staged smoke tests (see table above)
```
