"""
obs_setup_music.py — add (or re-apply) looping background music + voice ducking
to the RUNNING OBS, without restarting Bello.

The music source and its sidechain-ducking compressor are defined in
config.yaml under obs.music. This applies them live; run it again any time you
tune those numbers.

    python tools/obs_setup_music.py
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import obsws_python as obs          # noqa: E402
from obs_control import setup_music  # noqa: E402

cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
o = cfg["obs"]
client = obs.ReqClient(host=o["host"], port=o["port"], password=o["password"])
setup_music(client, o["scene"], o.get("music"))
print("done.")
