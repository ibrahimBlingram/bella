"""
tts.py  —  Bella voice layer (Phase 1: fully local via Kokoro)
==============================================================

Provider-agnostic TTS. Flip ONE line to swap engines:
    PROVIDER = "kokoro"      # fully local, free, English  (Phase-1 default)
    PROVIDER = "elevenlabs"  # cloud, 75ms, costs per char (fallback)

Both yield raw PCM16 / mono / 24 kHz so the downstream sink never changes
when you move to OBS in Phase 2.

Apple-Silicon hardening (handled automatically below, do not remove):
  - Locates Homebrew's libespeak-ng.dylib and points phonemizer at it, so
    Kokoro's grapheme->phoneme step doesn't die with "espeak not installed".
  - Sets PYTORCH_ENABLE_MPS_FALLBACK=1 so any op Metal lacks falls back to CPU
    instead of crashing (this is exactly what killed the VibeVoice attempt).
"""

from __future__ import annotations

import os
import glob
import asyncio
from typing import AsyncIterator

SAMPLE_RATE = 24_000
PROVIDER = "kokoro"

# --- Kokoro settings ---
KOKORO_VOICE = "af_heart"      # American-English female. af_bella / af_nicole / bf_emma ...
KOKORO_LANG = "a"              # 'a' American English, 'b' British English

# --- ElevenLabs settings (only used if PROVIDER == "elevenlabs") ---
ELEVEN_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
ELEVEN_MODEL = "eleven_flash_v2_5"


# ============================================================================
# Apple-Silicon environment fixes — run ONCE at import, before kokoro loads
# ============================================================================

def _harden_macos_env() -> None:
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    if os.environ.get("PHONEMIZER_ESPEAK_LIBRARY"):
        return  # user already set it; respect that

    candidates = (
        glob.glob("/opt/homebrew/Cellar/espeak-ng/*/lib/libespeak-ng.1.dylib")
        + glob.glob("/opt/homebrew/lib/libespeak-ng.1.dylib")
        + glob.glob("/usr/local/Cellar/espeak-ng/*/lib/libespeak-ng.1.dylib")
        + glob.glob("/usr/local/lib/libespeak-ng.1.dylib")
    )
    if not candidates:
        return  # modern kokoro often bundles espeak via espeakng-loader; fine.

    lib = sorted(candidates)[-1]
    os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = lib
    try:
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
        EspeakWrapper.set_library(lib)
    except Exception:
        pass  # misaki/espeakng-loader path will handle it instead


_harden_macos_env()


# ============================================================================
# Providers
# ============================================================================

class TTSProvider:
    async def stream(self, text: str) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield b""


class KokoroProvider(TTSProvider):
    def __init__(self) -> None:
        import numpy as np
        from kokoro import KPipeline
        self._np = np
        # No device kwarg: let it pick, MPS_FALLBACK covers any gap. CPU is
        # plenty for 82M and is the most reliable path on M-series.
        self._pipeline = KPipeline(lang_code=KOKORO_LANG)

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        np = self._np
        loop = asyncio.get_running_loop()

        def _generate() -> list[bytes]:
            chunks: list[bytes] = []
            for result in self._pipeline(text, voice=KOKORO_VOICE):
                # kokoro yields a (graphemes, phonemes, audio) tuple per segment
                audio = result[2] if isinstance(result, tuple) else result.audio
                if hasattr(audio, "detach"):          # torch tensor -> numpy
                    audio = audio.detach().cpu().numpy()
                audio = np.asarray(audio, dtype=np.float32)
                pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
                chunks.append(pcm16.tobytes())
            return chunks

        for chunk in await loop.run_in_executor(None, _generate):
            yield chunk


class ElevenLabsProvider(TTSProvider):
    def __init__(self) -> None:
        from elevenlabs.client import ElevenLabs
        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set.")
        self._client = ElevenLabs(api_key=key)

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        _DONE = object()

        def _produce():
            try:
                for chunk in self._client.text_to_speech.convert(
                    text=text, voice_id=ELEVEN_VOICE_ID,
                    model_id=ELEVEN_MODEL, output_format="pcm_24000",
                ):
                    if chunk:
                        asyncio.run_coroutine_threadsafe(q.put(chunk), loop).result()
            except Exception as e:
                asyncio.run_coroutine_threadsafe(q.put(e), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(q.put(_DONE), loop).result()

        loop.run_in_executor(None, _produce)
        while True:
            item = await q.get()
            if item is _DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item


def make_provider(name: str = PROVIDER) -> TTSProvider:
    name = name.lower()
    if name == "kokoro":
        return KokoroProvider()
    if name == "elevenlabs":
        return ElevenLabsProvider()
    raise ValueError(f"Unknown TTS provider: {name!r}")


# ============================================================================
# Speaker — persistent output stream so sentences play back-to-back smoothly
# ============================================================================

class Speaker:
    """Opens the audio device once and streams PCM into it. In Phase 2 you
    swap RawOutputStream for your OBS / virtual-cable route; nothing else
    changes."""

    def __init__(self, provider: str = PROVIDER) -> None:
        import sounddevice as sd
        self._provider = make_provider(provider)
        self._stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16"
        )
        self._stream.start()

    async def say(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        loop = asyncio.get_running_loop()
        async for pcm in self._provider.stream(text):
            await loop.run_in_executor(None, self._stream.write, pcm)

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


# ============================================================================
# Smoke test:  python tts.py "hello, I'm Bella"
# ============================================================================

if __name__ == "__main__":
    import sys
    line = " ".join(sys.argv[1:]) or "Hey everyone, welcome to the stream! I'm Bella."
    print(f"[tts] provider={PROVIDER}  rate={SAMPLE_RATE}")

    async def _main():
        spk = Speaker()
        await spk.say(line)
        spk.close()

    asyncio.run(_main())
