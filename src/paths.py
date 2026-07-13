"""
paths.py — make config paths work on any machine.

config.yaml stores paths RELATIVE to the repo root (clips/..., data/...). Those
have to become absolute before they're used, for two reasons:

  * OBS is a separate process with its own cwd — a relative "local_file" means
    nothing to it, so media/image sources must be handed absolute paths.
  * The repo lives at a different place on every box (a Mac vs /root/bella on
    Vast.ai), so hardcoding either one breaks the other.

Resolving here, at the point of use, means every entry point (main.py, the
test_*.py scripts, scripts/setup_obs_scene.py) gets it for free.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def abspath(p):
    """Absolute path for one config value. Absolute input is returned as-is, so
    an existing absolute path in config.yaml keeps working."""
    if not p:
        return p
    path = Path(p)
    return str(path if path.is_absolute() else (ROOT / path))


def abspaths(items):
    """abspath() over a list (avatar loops, demo backgrounds)."""
    return [abspath(p) for p in (items or [])]
