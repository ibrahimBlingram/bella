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
        print("[voice] Chatterbox Turbo (English) ready on cuda "
              "— performs [laugh]/[chuckle]/[sigh] as real sounds")

    def _synth_sync(self, text):
        # No exaggeration: Turbo ignores it (see class docstring).
        wav = self.model.generate(text, audio_prompt_path=self.ref)
        return _tensor_to_pcm16(wav)

    async def synth(self, text: str, lang: str = "en"):
        try:
            pcm = await asyncio.to_thread(self._synth_sync, text)
        except Exception as e:
            print(f"[voice] Chatterbox (Turbo) failed: {e}")
            return
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
        print(f"[voice] Chatterbox Multilingual (Arabic + English) ready on cuda "
              f"(emotion {self.exaggeration} .. {self.excited})")

    def _synth_sync(self, text, lang_id, ref):
        wav = self.model.generate(
            text, language_id=lang_id, audio_prompt_path=ref,
            exaggeration=emphasis_for(text, self.exaggeration, self.excited))
        return _tensor_to_pcm16(wav)

    async def synth(self, text: str, lang: str = "en"):
        lang_id = "ar" if lang == "ar" else "en"
        ref = self.ref_ar if lang_id == "ar" else self.ref_en
        try:
            pcm = await asyncio.to_thread(self._synth_sync, text, lang_id, ref)
        except Exception as e:
            print(f"[voice] Chatterbox (Multilingual, {lang_id}) failed: {e}")
            return
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

        # Separate Arabic engine — only when the primary can't speak Arabic.
        #
        # The best combo is chatterbox (Turbo) for English + chatterbox_multi for
        # Arabic: English gets REAL performed laughter ([laugh]/[sigh]/[chuckle],
        # which only Turbo can do) while Arabic still gets the same cloned voice
        # from the multilingual model. Two models on the GPU instead of one; that
        # is the price of laughter in English and a clone in Arabic.
        #
        # say() picks the engine per language and decides tag-stripping per
        # engine, so a tag can never leak into the Arabic model, which would
        # speak it as a literal word.
        self.arabic = None
        if not self.multilingual:
            ar = cfg["tts"].get("arabic") or {}
            if ar.get("enabled"):
                engine = (ar.get("engine") or "edge").lower()
                try:
                    if engine in ("chatterbox_multi", "chatterbox"):
                        if not _has_cuda():
                            raise RuntimeError("chatterbox Arabic needs CUDA")
                        self.arabic = ChatterboxMultiTTS(cfg)   # cloned Arabic voice
                    elif engine == "elevenlabs":         # paid, optional
                        self.arabic = ElevenLabsTTS(
                            voice_id=ar.get("elevenlabs_voice_id") or ar["voice_id"],
                            model_id=ar.get("model_id", "eleven_flash_v2_5"),
                            sample_rate=self.sr,
                        )
                    else:                                  # edge-tts: free, no key
                        self.arabic = EdgeTTS(ar.get("voice_id"), self.sr,
                                              label="Arabic")
                    print(f"[voice] Arabic enabled via {engine}")
                except Exception as e:
                    print(f"[voice] Arabic via {engine} failed ({e}); "
                          f"falling back to edge-tts.")
                    try:
                        self.arabic = EdgeTTS(ar.get("voice_id"), self.sr,
                                              label="Arabic")
                    except Exception as e2:
                        print(f"[voice] Arabic disabled ({e2}); English only.")
        self.has_arabic = self.multilingual or self.arabic is not None
        # True only when the ENGLISH engine can perform [laugh]/[sigh] as sounds.
        # The brain is told to write them only when this is on.
        self.performs_tags = bool(getattr(self.english, "performs_tags", False))
        self.speaking = asyncio.Event()

    def _engine(self, lang):
        if self.multilingual:                # one model handles every language
            return self.english
        return self.arabic if (lang == "ar" and self.arabic) else self.english

    async def say(self, sentences, lang="en", on_start=None, on_stop=None):
        engine = self._engine(lang)

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
                    async for pcm in engine.synth(sentence, lang):
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
                    # is ready. Non-streaming engines (Chatterbox) take ~30s to
                    # synthesize; opening the stream earlier just starves the
                    # buffer and spams ALSA underruns. 'high' latency gives a
                    # roomy buffer so playback stays smooth.
                    started = True
                    stream = sd.RawOutputStream(
                        samplerate=self.sr, channels=1, dtype="int16",
                        device=self.device, latency="high",
                    )
                    stream.start()
                    self.speaking.set()
                    if on_start:
                        on_start()
                await asyncio.to_thread(stream.write, pcm)
            await producer          # re-raise anything the producer swallowed
        finally:
            if not producer.done():
                producer.cancel()
            if stream is not None:
                await asyncio.to_thread(stream.stop)
                stream.close()
            self.speaking.clear()
            if started and on_stop:
                on_stop()
