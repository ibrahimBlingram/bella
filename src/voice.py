"""
voice.py — streaming TTS with per-language routing.

- English (and everything non-Arabic): Kokoro, local, free.
- Arabic: ElevenLabs (e.g. the "Sara" voice), only if tts.arabic.enabled and a
  key is set. If Arabic isn't enabled, callers keep using English/Kokoro.

Both engines output PCM16 mono @ tts.sample_rate (24000), so one playback path
handles either. espeak-ng is auto-located on Windows/Mac/Linux.
"""
import os
import glob
import platform
import asyncio

import numpy as np
import sounddevice as sd


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

    async def synth(self, text: str):
        def gen():
            for result in self.pipeline(text, voice=self.voice):
                audio = result[2] if isinstance(result, tuple) else result.audio
                if hasattr(audio, "detach"):
                    audio = audio.detach().cpu().numpy()
                audio = np.asarray(audio, dtype=np.float32)
                yield (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        async for chunk in _aiter(gen()):
            yield chunk


class ElevenLabsTTS:
    """Cloud TTS. Used here for Arabic (multilingual model). PCM16 @ sample_rate."""
    def __init__(self, voice_id, model_id, sample_rate):
        from elevenlabs.client import ElevenLabs
        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY not set (needed for Arabic).")
        self.client = ElevenLabs(api_key=key)
        self.voice = voice_id
        self.model = model_id
        self.sr = sample_rate

    async def synth(self, text: str):
        def gen():
            return self.client.text_to_speech.convert(
                voice_id=self.voice, model_id=self.model, text=text,
                output_format=f"pcm_{self.sr}",
            )
        async for chunk in _aiter(iter(gen())):
            yield chunk


# --------------------------------------------------------------------------
# Voice manager (routes by language)
# --------------------------------------------------------------------------
class Voice:
    def __init__(self, cfg):
        self.sr = cfg["tts"]["sample_rate"]
        self.device = cfg["tts"]["output_device"]
        self.english = KokoroTTS(cfg)            # primary

        self.arabic = None
        ar = cfg["tts"].get("arabic") or {}
        if ar.get("enabled"):
            try:
                self.arabic = ElevenLabsTTS(
                    voice_id=ar["voice_id"],
                    model_id=ar.get("model_id", "eleven_flash_v2_5"),
                    sample_rate=self.sr,
                )
            except Exception as e:
                print(f"[voice] Arabic disabled ({e}); falling back to English only.")
        self.has_arabic = self.arabic is not None
        self.speaking = asyncio.Event()

    def _engine(self, lang):
        return self.arabic if (lang == "ar" and self.arabic) else self.english

    async def say(self, sentences, lang="en", on_start=None, on_stop=None):
        engine = self._engine(lang)
        started = False
        stream = sd.RawOutputStream(
            samplerate=self.sr, channels=1, dtype="int16", device=self.device
        )
        stream.start()
        try:
            async for sentence in sentences:
                async for pcm in engine.synth(sentence):
                    if not started:
                        started = True
                        self.speaking.set()
                        if on_start:
                            on_start()
                    await asyncio.to_thread(stream.write, pcm)
        finally:
            await asyncio.to_thread(stream.stop)
            stream.close()
            self.speaking.clear()
            if started and on_stop:
                on_stop()
