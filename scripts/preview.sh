#!/bin/bash
# preview.sh — see what is on the stream RIGHT NOW, from a terminal.
#
# A terminal can't render a GUI, so this does the two things a terminal CAN do:
#
#   1. Saves the actual composited frame — exactly what a viewer would see — to
#      /workspace/preview.png. Open that in the Jupyter file browser (click it)
#      and Jupyter renders it as an image. No VNC, no tunnel, no browser plugin.
#
#   2. Prints the scene state as text: which project is on screen, the title, and
#      whether Bello is talking right now.
#
#     bash scripts/preview.sh          # one snapshot
#     bash scripts/preview.sh watch    # refresh every 3s until Ctrl-C
#
# For the full clickable OBS window instead, tunnel in and use noVNC:
#     ssh -f -N -L 8081:localhost:16006 -p 27252 root@76.121.3.151
#     open http://localhost:8081/vnc.html      (password: bello388)
set -u
cd "$(dirname "$0")/.."
source /venv/main/bin/activate 2>/dev/null

MODE="${1:-once}"

python - "$MODE" <<'PY'
import sys, time, yaml
import obsws_python as obs

mode = sys.argv[1]
cfg = yaml.safe_load(open("config.yaml")); o = cfg["obs"]
OUT = "/workspace/preview.png"

try:
    c = obs.ReqClient(host=o["host"], port=o["port"], password=o["password"])
except Exception as e:
    print(f"Can't reach OBS on :{o['port']} — is it running?  ({e})")
    print("Fix:  bash scripts/start_all.sh")
    sys.exit(1)


def visible(src):
    try:
        iid = c.get_scene_item_id(o["scene"], src).scene_item_id
        return c.get_scene_item_enabled(o["scene"], iid).scene_item_enabled
    except Exception:
        return False


def snapshot():
    v = c.get_video_settings()
    # The composited scene — what a viewer actually sees, not the OBS UI.
    c.save_source_screenshot(o["scene"], "png", OUT, v.base_width, v.base_height, 90)

    bg = (c.get_input_settings(o["background_source"]).input_settings.get("file") or "")
    bg = bg.split("/clips/")[-1] if "/clips/" in bg else bg
    try:
        title = c.get_input_settings(o["title_source"]).input_settings.get("text") or ""
    except Exception:
        title = ""
    talking = visible(o["avatar_talk_source"])
    live = c.get_stream_status().output_active

    print(f"  on screen : {bg}")
    print(f"  title     : {title.replace(chr(10), ' | ') or '(hidden)'}")
    print(f"  avatar    : {'TALKING' if talking else 'idle'}")
    print(f"  broadcast : {'LIVE' if live else 'not streaming'}")
    print(f"  frame     : {OUT}  <- click this in the Jupyter file browser to view")


if mode == "watch":
    print("Refreshing every 3s — Ctrl-C to stop.\n")
    try:
        while True:
            snapshot()
            print()
            time.sleep(3)
    except KeyboardInterrupt:
        print("stopped.")
else:
    snapshot()
PY
