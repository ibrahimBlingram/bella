"""
voice.py — streaming TTS with per-language routing.

The primary engine is chosen by tts.provider in config.yaml:

- edge             : Microsoft neural voices, English only, FREE, no key, no GPU.
                     Cloud — needs internet. Arabic routes to its own edge voice.
- kokoro           : local 82M model, English only, free (Mac/CPU/CUDA).
- chatterbox       : Chatterbox Turbo (350M), English only, CUDA only. Supports
                     inline paralinguistic tags ([laugh], [cough], [sigh],
                     [chuckle]) and zero-shot voice cloning from a reference clip.
- chatterbox_multi : Chatterbox Multilingual V3 (500M), 23 languages incl.
                     Arabic + English, CUDA only. One model serves both languages
                     (routed by lang) with per-language reference audio.

For English-only primaries (kokoro, chatterbox), Arabic comments route to a
separate engine when tts.arabic.enabled:
- edge       : Microsoft neural voices, FREE, no key (default).
- elevenlabs : paid cloud fallback.

Every engine's synth(text, lang) yields raw PCM16 mono bytes @ tts.sample_rate
(24000), so one playback path handles all of them. espeak-ng (Kokoro) is
auto-located on Windows/Mac/Linux; Chatterbox requires CUDA.
"""
import os
import glob
import platform
import asyncio
import re
import time

import numpy as np
import sounddevice as sd

# --- expressiveness -------------------------------------------------------
# Chatterbox takes an `exaggeration` per generate() call, so delivery doesn't
# have to be flat across a whole segment. A punchline should land harder than a
# price quote. We read the intensity off the punctuation the brain already
# writes: "!" = excited, "?" = curious lift, plain "." = the baseline.
#
# Chatterbox's usable range is ~0.3 (flat) .. ~1.0 (intense); past ~0.9 it tends
# to get shouty and artifact-y, so `excited` is capped rather than maxed.
_EXCITED = re.compile(
    r"!|\b(wow|omg|whoa|insane|crazy|unreal|amazing|stunning|obsessed|no way)\b",
    re.IGNORECASE)
_CURIOUS = re.compile(r"\?\s*$")

# Longest we will wait to OPEN or STOP the audio device before deciding it is gone.
# Nothing here should ever hang forever — a 24/7 stream that goes silently mute is
# worse than one that speaks a bad line.
_AUDIO_TIMEOUT = 8.0

# Audio is written to the device in small CHUNKS, not one blob per sentence. A
# whole-sentence write() only returns once its audio has DRAINED — it blocks for
# the LENGTH of the line — so a single-shot timeout had to be huge (20s) to avoid
# false alarms on long lines, which meant a genuinely dead device froze the stream
# for a full 20s before recovering. Chunking decouples "how long is this line"
# from "is the device dead": each chunk blocks ~its own duration, so a short
# per-chunk timeout catches a wedge in seconds without tripping on long lines.
_CHUNK_SECONDS = 0.4
_WRITE_TIMEOUT = 4.0


def _time_stretch(pcm: bytes, sr: int, speed: float) -> bytes:
    """Slow down (speed<1.0) or speed up PCM WITHOUT changing pitch, via ffmpeg's
    atempo filter. Chatterbox has no speed control — Turbo ignores every pacing
    knob — so time-stretching the finished audio is the only way to make him talk
    slower without turning him into a chipmunk or a baritone. On ANY failure we
    return the ORIGINAL audio: a wrong speed beats dropped speech on a live stream."""
    if not pcm or abs(speed - 1.0) < 1e-3:
        return pcm
    try:
        import subprocess
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        p = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error",
             "-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
             "-filter:a", f"atempo={speed:.4f}",
             "-f", "s16le", "-ar", str(sr), "-ac", "1", "pipe:1"],
            input=pcm, capture_output=True)
        return p.stdout or pcm
    except Exception as e:
        print(f"[voice] speed adjust skipped ({e}); playing at normal speed")
        return pcm


def emphasis_for(text: str, base: float, excited: float) -> float:
    """Per-sentence exaggeration. Falls back to `base` for ordinary lines."""
    if _EXCITED.search(text or ""):
        return excited
    if _CURIOUS.search(text or ""):
        return min(excited, base + 0.08)      # a small lift, not a full punch
    return base


# Stage directions the brain shouldn't emit, but sometimes does: "[laugh]",
# "*laughs*", "(sighs)". Only Chatterbox Turbo performs them as sounds; every
# other engine SPEAKS THE WORD on air. Strip them before they reach the voice.
#
# Brackets and markdown asterisks never belong in spoken copy, so those go
# unconditionally. Parentheses DO appear in real sentences ("Prices (starting)
# are great") — only remove those that name an actual performed sound.
_SOUND = (r"laugh|laughs|laughing|chuckle|chuckles|chuckling|sigh|sighs|"
          r"sighing|cough|coughs|coughing|clears throat|gasp|gasps|pause|beat")
_STAGE_DIRECTION = re.compile(
    r"\[[^\]]{1,20}\]"          # [laugh], [chuckle] — brackets are never spoken
    r"|\*[^*]{1,20}\*"          # *laughs* — markdown, persona forbids it anyway
    rf"|\((?:{_SOUND})\)",      # (sighs) but NOT (starting)
    re.IGNORECASE,
)


def strip_stage_directions(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _STAGE_DIRECTION.sub("", text)).strip()


# Foreign scripts that sometimes leak from the LLM into an Arabic reply (CJK,
# Hangul, Devanagari, Japanese kana). They are ALWAYS an error in Arabic copy and
# the voice would gibber them, so they're removed outright before synthesis.
_FOREIGN_SCRIPTS = re.compile(r"[一-鿿가-힯ऀ-ॿ"
                              r"぀-ヿ]")


def normalize_arabic(text: str) -> str:
    """Make Arabic text speak cleanly: prices in Arabic WORDS (not 'AED'/'M'/'K'),
    and no stray foreign-script characters. A safety net behind the prompt — the
    model is told to do this, but a viewer hears whatever slips through, so we also
    enforce it here right before the voice speaks."""
    t = text
    # Prices: 'AED 1.83 M' / 'AED 250K' / 'AED 900,000' -> Arabic words, right order.
    t = re.sub(r"(?i)\bAED\s*([\d.,]+)\s*M\b", r"\1 مليون درهم", t)
    t = re.sub(r"(?i)\bAED\s*([\d.,]+)\s*K\b", r"\1 ألف درهم", t)
    t = re.sub(r"(?i)\bAED\s*([\d.,]+)", r"\1 درهم", t)
    # Bare number + M/K (millions/thousands) with no AED in front.
    t = re.sub(r"([\d.,]+)\s*M\b", r"\1 مليون", t)
    t = re.sub(r"([\d.,]+)\s*K\b", r"\1 ألف", t)
    # Any leftover currency/unit tokens.
    t = re.sub(r"(?i)\bAED\b", "درهم", t)
    t = re.sub(r"(?i)\bsq\.?\s*ft\b|\bsqft\b", "قدم مربع", t)
    # Brand/developer names in Arabic so they aren't spoken in English mid-sentence.
    t = re.sub(r"(?i)\bSobha\s+Realty\b", "صبها العقارية", t)
    t = re.sub(r"(?i)\bSobha\b", "صبها", t)
    # Drop leaked foreign scripts. Then, since this is Arabic-only copy, any Latin
    # letters left are a model slip (a stray English word) — strip them so the voice
    # never breaks into English gibberish. Digits stay (prices already made words,
    # but plain numbers are fine for the Arabic voice to read).
    t = _FOREIGN_SCRIPTS.sub("", t)
    t = re.sub(r"[A-Za-z]+", "", t)
    return re.sub(r"\s{2,}", " ", t).strip()


# Which tts.* config block configures each EXTRA (non-English) language, and the
# language code the rest of the app routes by. English is served by the primary
# engine (tts.provider); every other language gets its own engine (Edge by default).
# Add a language by adding a block here and a matching block in config.yaml.
_LANG_BLOCKS = {"arabic": "ar", "chinese": "zh", "russian": "ru"}


def normalize_for(text: str, lang: str) -> str:
    """Per-language cleanup applied right before TTS. Arabic needs prices turned
    into words and leaked foreign scripts stripped (normalize_arabic); the Edge
    neural voices for Chinese and Russian read their own native script cleanly, so
    those pass through untouched."""
    return normalize_arabic(text) if lang == "ar" else text


def _harden_env():
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    if os.environ.get("PHONEMIZER_ESPEAK_LIBRARY"):
        return
    system = platform.system()
    if system == "Windows":
        cands = [r"C:\Program Files\eSpeak NG\libespeak-ng.dll",
                 r"C:\Program Files (x86)\eSpeak NG\libespeak-ng.dll"]
    elif system == "Darwin":
        cands = (glob.glob("/opt/homebrew/Cellar/espeak-ng/*/lib/libespeak-ng.1.dylib")
                 + ["/opt/homebrew/lib/libespeak-ng.1.dylib",
                    "/usr/local/lib/libespeak-ng.1.dylib"])
    else:
        cands = ["/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1",
                 "/usr/lib/libespeak-ng.so.1"]
    for c in cands:
        if os.path.exists(c):
            os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = c
            try:
                from phonemizer.backend.espeak.wrapper import EspeakWrapper
                EspeakWrapper.set_library(c)
            except Exception:
                pass
            return


_harden_env()


async def _aiter(sync_gen):
    while True:
        item = await asyncio.to_thread(next, sync_gen, None)
        if item is None:
            return
        yield item


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------
class KokoroTTS:
    """Local 82M model. PCM16 mono @ 24 kHz. No network, no key. English."""
    def __init__(self, cfg):
        import torch
        from kokoro import KPipeline
        tts = cfg["tts"]
        self.voice = tts.get("voice_id") or "af_heart"
        lang = tts.get("kokoro_lang", "a")
        if tts.get("sample_rate") != 24000:
            print("[voice] WARNING: Kokoro outputs 24000 Hz — set tts.sample_rate 24000.")
        # Kokoro's auto-detect only knows CUDA/CPU; on Apple Silicon force MPS
        # (the GPU) — ~2x faster than CPU. espeak/STFT ops fall back to CPU via
        # PYTORCH_ENABLE_MPS_FALLBACK (set in _harden_env).
        device = "mps" if torch.backends.mps.is_available() else None
        try:
            self.pipeline = KPipeline(lang_code=lang, device=device)
        except Exception as e:
            print(f"[voice] Kokoro on {device} failed ({e}); using CPU.")
            self.pipeline = KPipeline(lang_code=lang, device="cpu")
        # Warm up: MPS compiles its kernels on the first call (~3s). Pay that
        # now during startup so Bello's first spoken line on air is instant.
        try:
            for _ in self.pipeline("Hi.", voice=self.voice):
                pass
        except Exception:
            pass
        print(f"[voice] Kokoro on {device or 'cpu'} (warmed up)")

    async def synth(self, text: str, lang: str = "en"):
        def gen():
            for result in self.pipeline(text, voice=self.voice):
                audio = result[2] if isinstance(result, tuple) else result.audio
                if hasattr(audio, "detach"):
                    audio = audio.detach().cpu().numpy()
                audio = np.asarray(audio, dtype=np.float32)
                yield (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        async for chunk in _aiter(gen()):
            yield chunk


class EdgeTTS:
    """Microsoft Edge neural voices — FREE, no API key. Serves English and/or
    Arabic; one instance speaks one voice, so Voice keeps a separate instance per
    language. Needs internet, like Gemini already does. Streams MP3 which we
    decode to PCM16 mono @ sample_rate with the ffmpeg bundled in imageio-ffmpeg.
    Resilient: a network blip logs and skips the line rather than crashing the
    stream."""
    def __init__(self, voice_id, sample_rate, label="edge"):
        import imageio_ffmpeg
        self.voice = voice_id or "ar-SA-ZariyahNeural"
        self.sr = sample_rate
        self.label = label
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    def _decode(self, mp3: bytes) -> bytes:
        import subprocess
        p = subprocess.run(
            [self.ffmpeg, "-i", "pipe:0", "-ar", str(self.sr), "-ac", "1",
             "-f", "s16le", "pipe:1"],
            input=mp3, capture_output=True)
        return p.stdout

    async def synth(self, text: str, lang: str = "en"):
        import edge_tts
        try:
            comm = edge_tts.Communicate(text, self.voice)
            mp3 = bytearray()
            async for ch in comm.stream():
                if ch["type"] == "audio":
                    mp3 += ch["data"]
        except Exception as e:
            print(f"[voice] {self.label} (edge-tts) failed: {e}")
            return
        if not mp3:
            return
        pcm = await asyncio.to_thread(self._decode, bytes(mp3))
        if pcm:
            yield pcm


class ElevenLabsTTS:
    """Cloud TTS (paid). Optional Arabic fallback. PCM16 @ sample_rate."""
    def __init__(self, voice_id, model_id, sample_rate):
        from elevenlabs.client import ElevenLabs
        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY not set (needed for Arabic).")
        self.client = ElevenLabs(api_key=key)
        self.voice = voice_id
        self.model = model_id
        self.sr = sample_rate

    async def synth(self, text: str, lang: str = "en"):
        def gen():
            return self.client.text_to_speech.convert(
                voice_id=self.voice, model_id=self.model, text=text,
                output_format=f"pcm_{self.sr}",
            )
        async for chunk in _aiter(iter(gen())):
            yield chunk


def _tensor_to_pcm16(wav) -> bytes:
    """Chatterbox returns a torch tensor -> raw PCM16 mono bytes."""
    audio = wav.squeeze().detach().cpu().numpy()
    audio = np.asarray(audio, dtype=np.float32)
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _resolve_ref(path, label):
    """Return a usable voice-clone reference path, or None if it's unset/missing.
    A missing file is fine — Chatterbox falls back to its built-in default voice,
    so you can go live before you've recorded Bella's clone refs."""
    from paths import abspath
    path = abspath(path)
    if path and os.path.exists(path):
        return path
    if path:
        print(f"[voice] {label} ref not found ({path}); using Chatterbox's "
              f"default voice. Drop the file in and restart to clone Bella.")
    else:
        print(f"[voice] no {label} ref set; using Chatterbox's default voice.")
    return None


class ChatterboxTurboTTS:
    """Chatterbox Turbo (350M) — English only, CUDA only, MIT-licensed.
    Zero-shot voice cloning from a 5-10s reference clip and inline paralinguistic
    tags ([laugh], [cough], [sigh], [chuckle]). PCM16 mono @ model.sr (24000).
    Not streaming: generates the whole line, then yields it once.

    Turbo does NOT support `exaggeration` — it logs
        "CFG, min_p and exaggeration are not supported by Turbo version
         and will be ignored"
    once per line and drops it. So we don't pass it: expressiveness in English
    comes from the paralinguistic TAGS (which Turbo alone performs), while
    exaggeration is what the Multilingual model uses for Arabic. The two engines
    are expressive in different ways, and each is fed only what it understands.
    """
    performs_tags = True        # the only engine that renders tags as sounds

    def __init__(self, cfg):
        from chatterbox.tts_turbo import ChatterboxTurboTTS as _Model
        tts = cfg["tts"]
        self.model = _Model.from_pretrained(device="cuda")
        self.ref = _resolve_ref(tts.get("chatterbox_ref_audio"), "English")
        self.sr = getattr(self.model, "sr", 24000)
        if tts.get("sample_rate") != self.sr:
            print(f"[voice] WARNING: Chatterbox outputs {self.sr} Hz — "
                  f"set tts.sample_rate {self.sr}.")
        # Warm up: the first generate() compiles CUDA kernels (~seconds). Pay that
        # NOW, at startup, so Bello's first spoken line on air isn't slow.
        try:
            self._synth_sync("Hello there.")
        except Exception:
            pass
        print("[voice] Chatterbox Turbo (English) ready on cuda "
              "— performs [laugh]/[chuckle]/[sigh] as real sounds")

    def _synth_sync(self, text):
        # No exaggeration: Turbo ignores it (see class docstring).
        wav = self.model.generate(text, audio_prompt_path=self.ref)
        return _tensor_to_pcm16(wav)

    async def synth(self, text: str, lang: str = "en"):
        t0 = time.perf_counter()
        try:
            pcm = await asyncio.to_thread(self._synth_sync, text)
        except Exception as e:
            print(f"[voice] Chatterbox (Turbo) failed: {e}")
            return
        print(f"[voice] EN synth {time.perf_counter() - t0:.2f}s")
        if pcm:
            yield pcm


class ChatterboxMultiTTS:
    """Chatterbox Multilingual V3 (500M) — 23 languages incl. Arabic + English,
    CUDA only, MIT-licensed. One model serves every language, routed by lang.
    Reference audio MUST match the target language (English ref for English,
    Arabic ref for Arabic) or the accent bleeds through. PCM16 mono @ model.sr."""
    def __init__(self, cfg):
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS as _Model
        tts = cfg["tts"]
        self.model = _Model.from_pretrained(device="cuda")
        self.ref_en = _resolve_ref(tts.get("chatterbox_ref_audio"), "English")
        self.ref_ar = _resolve_ref(
            tts.get("chatterbox_ref_audio_ar"), "Arabic") or self.ref_en
        self.exaggeration = float(tts.get("chatterbox_exaggeration", 0.7))
        self.excited = float(tts.get("chatterbox_exaggeration_excited",
                                     min(1.0, self.exaggeration + 0.18)))
        self.sr = getattr(self.model, "sr", 24000)
        if tts.get("sample_rate") != self.sr:
            print(f"[voice] WARNING: Chatterbox outputs {self.sr} Hz — "
                  f"set tts.sample_rate {self.sr}.")
        # Warm up BOTH language paths now so the FIRST Arabic comment (and the first
        # English one) doesn't pay the cold-start kernel-compile cost live. This is
        # a big part of the "delay when it switches to Arabic" — the Arabic path was
        # ice cold until the first Arabic viewer, who then waited on the warm-up.
        try:
            self._synth_sync("Hello there.", "en", self.ref_en)
            self._synth_sync("مرحبا بكم.", "ar", self.ref_ar)
        except Exception:
            pass
        print(f"[voice] Chatterbox Multilingual (Arabic + English) ready on cuda "
              f"(emotion {self.exaggeration} .. {self.excited})")

    def _synth_sync(self, text, lang_id, ref):
        # Constant, low exaggeration keeps the delivery ON the cloned reference
        # voice; a high value drifts off it (that was the wandering-accent bug).
        wav = self.model.generate(
            text, language_id=lang_id, audio_prompt_path=ref,
            exaggeration=emphasis_for(text, self.exaggeration, self.excited))
        return _tensor_to_pcm16(wav)

    async def synth(self, text: str, lang: str = "en"):
        lang_id = "ar" if lang == "ar" else "en"
        ref = self.ref_ar if lang_id == "ar" else self.ref_en
        t0 = time.perf_counter()
        try:
            pcm = await asyncio.to_thread(self._synth_sync, text, lang_id, ref)
        except Exception as e:
            print(f"[voice] Chatterbox (Multilingual, {lang_id}) failed: {e}")
            return
        print(f"[voice] {lang_id.upper()} synth {time.perf_counter() - t0:.2f}s")
        if pcm:
            yield pcm


# --------------------------------------------------------------------------
# Voice manager (routes by language)
# --------------------------------------------------------------------------
def _has_cuda() -> bool:
    """Chatterbox is CUDA-only (no CPU, no MPS). Checked before we try to load it
    so the same config.yaml can run on the GPU server AND on a Mac."""
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False                # torch isn't even installed (Mac path)


class Voice:
    def __init__(self, cfg):
        self.sr = cfg["tts"]["sample_rate"]
        self.device = cfg["tts"]["output_device"]
        # Playback speed (pitch-preserved). <1.0 = slower. See _time_stretch.
        # Arabic gets its own (usually slower) speed for clarity — it's the primary
        # language and must be crystal-clear. Falls back to `speed` if unset.
        self.speed = float(cfg["tts"].get("speed", 1.0))
        self.speed_ar = float(cfg["tts"].get("speed_ar", self.speed))
        provider = (cfg["tts"].get("provider") or "kokoro").lower()

        # config.yaml is set for the GPU server (chatterbox_multi). On a machine
        # with no CUDA — a Mac — fall back to edge-tts instead of crashing, so one
        # config works in both places. The server has CUDA and uses Chatterbox.
        if provider.startswith("chatterbox") and not _has_cuda():
            print(f"[voice] '{provider}' needs CUDA and this machine has none — "
                  f"falling back to edge-tts. (The GPU server will use Chatterbox.)")
            provider = "edge"

        # Primary engine. chatterbox_multi is multilingual — the same model also
        # serves Arabic (routed by lang), so no separate Arabic engine is needed.
        self.multilingual = False
        if provider == "chatterbox":
            self.english = ChatterboxTurboTTS(cfg)     # English only, CUDA
        elif provider == "chatterbox_multi":
            self.english = ChatterboxMultiTTS(cfg)     # Arabic + English, CUDA
            self.multilingual = True
        elif provider == "edge":
            self.english = EdgeTTS(                    # free cloud, no key, no GPU
                cfg["tts"].get("edge_voice_id") or "en-US-AndrewNeural",
                self.sr, label="English")
            print(f"[voice] English via edge-tts ({self.english.voice})")
        else:
            self.english = KokoroTTS(cfg)              # local, English only

        # Separate engine PER extra (non-English) language — only when the primary
        # can't speak them. Arabic, Chinese, and Russian each get their own voice,
        # chosen in config (tts.arabic / tts.chinese / tts.russian). Edge's clear
        # neural voices are the default: the same clarity-over-cloning call that put
        # Arabic on Edge applies to tonal Chinese and to Russian too. English keeps
        # Chatterbox Turbo (cloned voice + REAL performed laughter). A multilingual
        # primary (chatterbox_multi) already covers every language, so no extras then.
        #
        # say() picks the engine per language and decides tag-stripping per engine,
        # so a paralinguistic tag can never leak into a non-English model, which
        # would speak it as a literal word.
        self.extra = {}
        if not self.multilingual:
            for block, code in _LANG_BLOCKS.items():
                spec = cfg["tts"].get(block) or {}
                if not spec.get("enabled"):
                    continue
                engine = self._build_lang_engine(cfg, spec, block)
                if engine is not None:
                    self.extra[code] = engine
        # Back-compat: main.py + tests still reference has_arabic / arabic.
        self.arabic = self.extra.get("ar")
        self.has_arabic = self.multilingual or ("ar" in self.extra)
        # True only when the ENGLISH engine can perform [laugh]/[sigh] as sounds.
        # The brain is told to write them only when this is on.
        self.performs_tags = bool(getattr(self.english, "performs_tags", False))
        self.speaking = asyncio.Event()
        # Set when the audio device misbehaves; makes the next stream open
        # re-initialise PortAudio so it stops handing out a dead device.
        self._audio_broken = False

    def _build_lang_engine(self, cfg, spec, label):
        """Build the TTS engine for one non-English language from its config block
        (tts.arabic / tts.chinese / tts.russian). Falls back to edge-tts on ANY
        failure — a clear neural voice beats a dead language on a live stream."""
        engine = (spec.get("engine") or "edge").lower()
        try:
            if engine in ("chatterbox_multi", "chatterbox"):
                if not _has_cuda():
                    raise RuntimeError(f"chatterbox {label} needs CUDA")
                eng = ChatterboxMultiTTS(cfg)            # cloned voice (CUDA)
            elif engine == "elevenlabs":                 # paid, optional
                eng = ElevenLabsTTS(
                    voice_id=spec.get("elevenlabs_voice_id") or spec["voice_id"],
                    model_id=spec.get("model_id", "eleven_flash_v2_5"),
                    sample_rate=self.sr,
                )
            else:                                        # edge-tts: free, no key
                eng = EdgeTTS(spec.get("voice_id"), self.sr, label=label)
            print(f"[voice] {label} enabled via {engine}")
            return eng
        except Exception as e:
            print(f"[voice] {label} via {engine} failed ({e}); falling back to edge-tts.")
            try:
                return EdgeTTS(spec.get("voice_id"), self.sr, label=label)
            except Exception as e2:
                print(f"[voice] {label} disabled ({e2}).")
                return None

    def has_lang(self, lang) -> bool:
        """Can Bello speak this language? English always; a multilingual primary
        covers everything; otherwise only the extra languages we built an engine for."""
        return self.multilingual or lang == "en" or lang in self.extra

    def _engine(self, lang):
        if self.multilingual:                # one model handles every language
            return self.english
        return self.extra.get(lang, self.english)

    async def say(self, sentences, lang="en", on_start=None, on_stop=None):
        engine = self._engine(lang)
        # English keeps its own (chatterbox) pace; Arabic has its own knob (clarity).
        # The other Edge neural voices (zh, ru) are already clear, so they play at
        # 1.0 — time-stretching a clean neural voice only adds robotic artifacts.
        speed = (self.speed if lang == "en"
                 else self.speed_ar if lang == "ar" else 1.0)

        # Synthesize AHEAD of playback. Non-streaming engines (edge, chatterbox)
        # emit one blob per sentence, so a serial synth->play->synth loop leaves
        # a synth-length silence between every sentence — ~1.5s of dead air on
        # edge. Running synthesis in its own task lets sentence N+1 generate
        # while N is still being written to the device. maxsize bounds the
        # lookahead: we never run the whole answer ahead of the speaker, and we
        # never synthesize lines a caller abandons mid-way.
        queue = asyncio.Queue(maxsize=2)
        DONE = object()

        # Turbo renders "[laugh]" as a laugh; everyone else would say the word.
        clean = not getattr(engine, "performs_tags", False)

        async def produce():
            try:
                async for sentence in sentences:
                    if clean:
                        sentence = strip_stage_directions(sentence)
                        if not sentence:
                            continue
                    if lang != "en":
                        # Per-language cleanup: Arabic -> prices in words + strip
                        # leaked foreign scripts (so the voice never says "AED"/"M"
                        # or gibbers a stray glyph); zh/ru pass through unchanged.
                        sentence = normalize_for(sentence, lang)
                        if not sentence:
                            continue
                    async for pcm in engine.synth(sentence, lang):
                        if speed != 1.0:
                            # Slow him down (pitch-preserved) OFF the event loop so
                            # audio already playing doesn't stutter while we stretch.
                            pcm = await asyncio.to_thread(
                                _time_stretch, pcm, self.sr, speed)
                        await queue.put(pcm)
            finally:
                await queue.put(DONE)

        producer = asyncio.create_task(produce())
        started = False
        stream = None
        try:
            while True:
                pcm = await queue.get()
                if pcm is DONE:
                    break
                if not started:
                    # Open the output stream LAZILY — only once the first audio
                    # is ready. Non-streaming engines (Chatterbox) take ~1s to
                    # synthesize; opening the stream earlier just starves the
                    # buffer and spams ALSA underruns. 'high' latency gives a
                    # roomy buffer so playback stays smooth.
                    #
                    # Both the OPEN and the WRITES are guarded. If the audio device
                    # goes away underneath us — the PulseAudio sink is recreated, a
                    # container hiccup, anything — PortAudio does not raise: it
                    # BLOCKS. That silently wedged the whole stream once: the write
                    # never returned, so speak() never released the speak-lock, and
                    # Bello went mute for good with not one line in the log. He must
                    # never again go quiet without saying why.
                    try:
                        stream = await asyncio.wait_for(
                            asyncio.to_thread(self._open_stream), timeout=_AUDIO_TIMEOUT)
                    except (asyncio.TimeoutError, Exception) as e:
                        what = "timed out" if isinstance(e, asyncio.TimeoutError) else e
                        self._audio_broken = True     # force a re-init next time
                        print(f"[voice] audio device would not open ({what}) — "
                              f"dropping this line, re-initialising before the next")
                        return
                    started = True
                    self.speaking.set()
                    if on_start:
                        on_start()
                # Write in small chunks (see _CHUNK_SECONDS): a wedged device is
                # caught in ~_WRITE_TIMEOUT seconds instead of freezing the stream
                # for the whole 20s a single-shot write used to allow.
                chunk = int(self.sr * _CHUNK_SECONDS) * 2   # int16 mono: 2 bytes/sample
                wedged = False
                for off in range(0, len(pcm), chunk):
                    frame = pcm[off:off + chunk]
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(stream.write, frame),
                            timeout=_WRITE_TIMEOUT)
                    except asyncio.TimeoutError:
                        # The device is wedged. Bail out of THIS line rather than
                        # hang holding the lock — the next line reopens the stream.
                        self._audio_broken = True     # force a re-init next time
                        print(f"[voice] audio write blocked >{_WRITE_TIMEOUT}s — "
                              f"device wedged; re-initialising the audio backend")
                        wedged = True
                        break
                    except Exception as e:
                        self._audio_broken = True
                        print(f"[voice] audio write failed ({e}); re-initialising")
                        wedged = True
                        break
                if wedged:
                    return
            await producer          # re-raise anything the producer swallowed
        finally:
            if not producer.done():
                producer.cancel()
            if stream is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(stream.stop), timeout=_AUDIO_TIMEOUT)
                except Exception:
                    pass            # a wedged device can hang stop() too
                try:
                    stream.close()
                except Exception:
                    pass
            self.speaking.clear()
            if started and on_stop:
                on_stop()

    def _open_stream(self):
        # PortAudio enumerates the audio devices ONCE, when it initialises. If the
        # PulseAudio sink is destroyed and recreated underneath us, PortAudio keeps
        # handing out a handle to the dead one — opening "works", writes block, and
        # no amount of retrying helps. Re-initialising is the only way to make it
        # see the new sink. We only pay that cost after a failure.
        if self._audio_broken:
            try:
                sd._terminate()
                sd._initialize()
                print("[voice] re-initialised the audio backend "
                      "(PortAudio was holding a dead device)")
            except Exception as e:
                print(f"[voice] audio re-init failed: {e}")
            self._audio_broken = False
        s = sd.RawOutputStream(
            samplerate=self.sr, channels=1, dtype="int16",
            device=self.device, latency="high",
        )
        s.start()
        return s
