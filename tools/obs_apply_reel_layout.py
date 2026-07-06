"""
obs_apply_reel_layout.py — make the LIVE OBS scene look like the exported reel.

Run this WITH OBS OPEN (WebSocket on the port in config.yaml). It:
  * scales the Background source to cover the whole canvas
  * crops both avatar loops to a waist-up bust, pinned bottom-right
  * creates a title/price text source and shows a sample line

Use it to apply + eyeball + tune the layout without starting the whole bot.
Re-run after tweaking the numbers below. Once it looks right, `python src/main.py`
will keep it that way (obs.reel_layout: true) and drive the title automatically.

    python tools/obs_apply_reel_layout.py            # apply + sample title
    python tools/obs_apply_reel_layout.py --clear    # hide the sample title
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from obs_control import OBS   # noqa: E402


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    o = cfg.get("obs") or {}
    obs = OBS(cfg)
    obs.title_src = o.get("title_source", "BellaTitle")

    print("Applying reel layout to scene:", obs.scene)
    obs.apply_reel_layout()

    if "--clear" in sys.argv:
        obs.hide_title()
        print("Title hidden.")
    else:
        obs.set_title("The Grove", "AED 9.32 M")
        print("Sample title shown. Tweak width_frac/keep_top in obs_control.py's "
              "bust_avatar() if the crop needs adjusting, then re-run.")


if __name__ == "__main__":
    main()
