"""
setup_obs_scene.py — build Bella's OBS scene programmatically (headless Vast.ai).

There's no GUI on Vast, so everything is created over the OBS WebSocket:

  * scene "Live" (from config.yaml obs.scene)
  * Media Source  AvatarIdle   (idle clip, looping)
  * Media Source  AvatarTalk   (talk clip, looping)
  * Image Source  Background    (background image)
  * Audio Input Capture of the PulseAudio null-sink monitor (bella_audio.monitor)
  * Chroma Key (green) filter on both avatar sources
  * Source order: AvatarTalk (top) -> AvatarIdle -> Background (bottom)
  * Stream settings: Custom/RTMP -> TikTok, key + URL from .env

Safe to re-run: it skips anything that already exists.

    DISPLAY=:99 python scripts/setup_obs_scene.py
"""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
import obsws_python as obs

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
o = cfg["obs"]

# PulseAudio null-sink monitor that OBS captures (created in setup_vast.sh).
AUDIO_DEVICE_ID = os.environ.get("BELLA_PULSE_MONITOR", "bella_audio.monitor")
AUDIO_SOURCE = "BellaAudio"

c = obs.ReqClient(host=o["host"], port=o["port"], password=o.get("password") or "")
scene = o["scene"]


def _inputs():
    return {i["inputName"] for i in c.get_input_list().inputs}


def _ensure_scene():
    scenes = [s["sceneName"] for s in c.get_scene_list().scenes]
    if scene not in scenes:
        c.create_scene(scene)
        print(f"created scene: {scene}")
    else:
        print(f"scene exists: {scene}")


def _create_input(name, kind, settings, enabled=True):
    if name in _inputs():
        print(f"source exists: {name}")
        # Keep its settings in step with config on re-run.
        c.set_input_settings(name, settings, overlay=True)
        return
    c.create_input(scene, name, kind, settings, enabled)
    print(f"created source: {name}  ({kind})")


def _chroma_key(source):
    """Green chroma key so the avatar clip composites over the background."""
    try:
        existing = {f["filterName"] for f in c.get_source_filter_list(source).filters}
        if "ChromaKey" in existing:
            return
    except Exception:
        pass
    try:
        c.create_source_filter(
            source, "ChromaKey", "chroma_key_filter_v2",
            {"key_color_type": "green", "similarity": 400, "smoothness": 80},
        )
        print(f"chroma key added: {source}")
    except Exception as e:
        print(f"[warn] chroma key on {source} skipped: {e}")


def _order(names_bottom_to_top):
    """index 0 == bottom of the stack."""
    for idx, name in enumerate(names_bottom_to_top):
        try:
            iid = c.get_scene_item_id(scene, name).scene_item_id
            c.set_scene_item_index(scene, iid, idx)
        except Exception as e:
            print(f"[warn] order {name} skipped: {e}")


def main():
    _ensure_scene()

    idle_clip = o["avatar_idle_loops"][0]
    talk_clip = o["avatar_talk_loops"][0]
    background = o["demo_backgrounds"][0]

    # Media sources (looping avatar clips). ffmpeg_source is the OBS media kind.
    _create_input(o["avatar_idle_source"], "ffmpeg_source",
                  {"local_file": idle_clip, "looping": True, "is_local_file": True},
                  enabled=True)
    _create_input(o["avatar_talk_source"], "ffmpeg_source",
                  {"local_file": talk_clip, "looping": True, "is_local_file": True},
                  enabled=False)   # hidden at start; main.py toggles talk/idle
    # Image background.
    _create_input(o["background_source"], "image_source",
                  {"file": background}, enabled=True)
    # Audio capture of the PulseAudio null-sink monitor (Bella's TTS output).
    _create_input(AUDIO_SOURCE, "pulse_input_capture",
                  {"device_id": AUDIO_DEVICE_ID}, enabled=True)

    # Chroma key both avatar clips (green screen -> transparent).
    _chroma_key(o["avatar_talk_source"])
    _chroma_key(o["avatar_idle_source"])

    # Stack: Background (bottom) -> AvatarIdle -> AvatarTalk (top).
    _order([o["background_source"], o["avatar_idle_source"], o["avatar_talk_source"]])

    # Make "Live" the active program scene.
    c.set_current_program_scene(scene)

    # Streaming settings -> TikTok via custom RTMP.
    server = os.environ.get("TIKTOK_SERVER_URL", "")
    key = os.environ.get("TIKTOK_STREAM_KEY", "")
    if server and key and "..." not in server:
        c.set_stream_service_settings(
            "rtmp_custom",
            {"server": server, "key": key, "use_auth": False},
        )
        print("stream settings set: Custom RTMP -> TikTok")
    else:
        print("[warn] TIKTOK_SERVER_URL / TIKTOK_STREAM_KEY not set — skipping "
              "stream config. Fill them in .env and re-run to enable streaming.")

    print(f"\n[OK] OBS scene '{scene}' ready. Start streaming with "
          "c.start_stream() or the OBS 'Start Streaming' button.")


if __name__ == "__main__":
    main()
