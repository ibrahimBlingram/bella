"""
record_test.py — capture the live OBS output to a file so you can verify the
whole pipeline (avatar + background + Bella's voice) WITHOUT streaming to TikTok.

Run WHILE src/main.py is running (so Bella is narrating and driving the avatar):

    python scripts/record_test.py 60      # record 60 seconds

Then download the printed file from the Jupyter file browser and play it — you
should SEE the talking avatar over the Sobha background and HEAR Bella.
"""
import sys
import time

import obsws_python as obs

SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
OUT_DIR = "/root/bella/recordings"

c = obs.ReqClient(host="localhost", port=4455, password="")

# Put recordings somewhere easy to find (best-effort; OBS 30+ supports this).
try:
    import os
    os.makedirs(OUT_DIR, exist_ok=True)
    c.set_record_directory(OUT_DIR)
except Exception as e:
    print(f"[rec] couldn't set record dir ({e}); using OBS default.")

try:
    where = c.get_record_directory().record_directory
except Exception:
    where = "(OBS default)"

c.start_record()
print(f"[rec] recording {SECS}s into {where} ... (Bella should be talking)")
time.sleep(SECS)
r = c.stop_record()

path = getattr(r, "output_path", None) or where
print(f"\n[rec] DONE. File saved at:\n    {path}\n"
      "Download it from the Jupyter file browser and play it.")
