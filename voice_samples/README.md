# voice_samples

Voice-clone reference audio for Chatterbox TTS (see `tts.chatterbox_ref_audio*`
in `config.yaml`). Chatterbox clones the voice zero-shot from a short clip:

- `bella_ref_en.wav` — 5-10s of clean English speech in Bella's target voice.
- `bella_ref_ar.wav` — 5-10s of clean Arabic speech in the same voice.

The reference language **must match** the synthesis language or the accent
bleeds through (an English ref speaking Arabic sounds wrong). Use mono, 24 kHz,
no background noise.

The `KOKORO_*.wav` files are sample outputs from the Kokoro voices, kept only for
reference/A-B comparison — they are not used by Chatterbox.
