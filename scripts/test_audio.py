"""
test_audio.py — prove the voice path works WITHOUT Chatterbox.

Records OBS for 6s while playing a 440Hz test tone through sounddevice (the same
path Bella's voice uses: sounddevice -> ALSA/pulse -> bella_audio sink -> OBS).
If you HEAR the tone in the saved file, audio routing is correct and any missing
voice is just Chatterbox being slow. If it's silent, routing is still broken.

    python scripts/test_audio.py
"""
import time

import numpy as np
import sounddevice as sd
import obsws_python as obs

SR = 24000
tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(SR * 5) / SR) * 32767).astype("int16")

c = obs.ReqClient(host="localhost", port=4455, password="")
c.start_record()
time.sleep(0.5)
print("[test_audio] playing 5s 440Hz tone into the default device...")
sd.play(tone.reshape(-1, 1), SR)
sd.wait()
time.sleep(0.5)
r = c.stop_record()
print(f"[test_audio] saved: {getattr(r, 'output_path', '(check recordings dir)')}")
print("Play it — if you HEAR a beep, audio routing works.")
