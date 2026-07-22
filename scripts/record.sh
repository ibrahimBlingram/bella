#!/bin/bash
# record.sh — start / stop / check a LOCAL OBS recording of the live scene.
#
# Records the exact program output (Bello + background + ticker + his voice) to
# /workspace/bella/recordings as a crash-safe hybrid MP4. This is INDEPENDENT of
# streaming: you can record whether or not you're broadcasting to TikTok, and it
# reuses the stream encoder so it costs almost no extra CPU.
#
#     bash scripts/record.sh start     # begin recording
#     bash scripts/record.sh stop      # stop AND print the saved file + how to download it
#     bash scripts/record.sh status    # am I recording? how long? what files exist?
#
# ALWAYS stop with this (not by killing OBS) so the MP4 is finalised cleanly.
set -u
cd "$(dirname "$0")/.."
source /venv/main/bin/activate 2>/dev/null

ACTION="${1:-status}"

python - "$ACTION" <<'PY'
import sys, time, os, glob
import yaml
import obsws_python as obs

action = sys.argv[1]
o = yaml.safe_load(open("config.yaml"))["obs"]
c = obs.ReqClient(host=o["host"], port=o["port"], password=o.get("password") or "")
RECDIR = "/workspace/bella/recordings"
os.makedirs(RECDIR, exist_ok=True)

def configure():
    # Where + what format. hybrid_mp4 survives an OBS crash yet still plays as MP4;
    # "Stream" quality reuses the stream encoder so there's no second encode.
    c.set_profile_parameter("SimpleOutput", "FilePath", RECDIR)
    c.set_profile_parameter("SimpleOutput", "RecQuality", "Stream")
    try:
        c.set_profile_parameter("SimpleOutput", "RecFormat2", "hybrid_mp4")
    except Exception:
        c.set_profile_parameter("SimpleOutput", "RecFormat2", "mp4")

if action == "start":
    if c.get_record_status().output_active:
        print("Already recording.")
    else:
        configure()
        c.start_record()
        time.sleep(2)
        print(f"Recording STARTED -> {RECDIR}")
    st = c.get_record_status()
    print(f"  active: {st.output_active}   timecode: {getattr(st,'output_timecode','')}")

elif action == "stop":
    if not c.get_record_status().output_active:
        print("Not recording — nothing to stop.")
    else:
        r = c.stop_record()
        time.sleep(1)
        path = getattr(r, "output_path", None) or "(newest file in the folder below)"
        print(f"Recording STOPPED. Saved: {path}")
        print("\nDownload it to your computer (run this on YOUR machine, fill in PORT/HOST):")
        print(f"  scp -P <PORT> root@<HOST>:{path} .")

else:  # status
    st = c.get_record_status()
    print(f"recording : {st.output_active}")
    print(f"timecode  : {getattr(st,'output_timecode','')}")
    print(f"folder    : {RECDIR}")
    files = sorted(glob.glob(RECDIR + "/*"))
    if files:
        print("files:")
        for f in files[-8:]:
            print(f"    {f}  ({os.path.getsize(f)//(1024*1024)} MB)")
    else:
        print("files: (none yet)")
PY
