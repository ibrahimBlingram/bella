#!/bin/bash
# stream.sh — start / stop / check the live RTMP broadcast.
#
# This ONLY controls the broadcast. Bello (src/main.py) and OBS keep running
# either way — stopping the stream just stops sending frames out, it doesn't
# tear anything down. So you can stop the stream, fix something, and start it
# again without a full restart.
#
#     bash scripts/stream.sh status    # am I live? dropped frames? destination?
#     bash scripts/stream.sh start     # GO LIVE
#     bash scripts/stream.sh stop      # stop broadcasting
#
# The RTMP destination (server + key) is whatever is set in OBS itself — set it
# in the OBS GUI over noVNC (Settings -> Stream), or in .env + setup_obs_scene.py.
set -u
cd "$(dirname "$0")/.."
source /venv/main/bin/activate 2>/dev/null

ACTION="${1:-status}"

python - "$ACTION" <<'PY'
import sys, time, yaml
import obsws_python as obs

action = sys.argv[1]
cfg = yaml.safe_load(open("config.yaml")); o = cfg["obs"]
try:
    c = obs.ReqClient(host=o["host"], port=o["port"], password=o["password"])
except Exception as e:
    print(f"Can't reach OBS on :{o['port']} — is it running?  ({e})")
    print("Fix:  bash scripts/start_all.sh")
    sys.exit(1)

def destination():
    ss = c.get_stream_service_settings(); s = ss.stream_service_settings
    key = str(s.get("key") or "")
    return s.get("server") or "<none>", key

def status(verbose=True):
    st = c.get_stream_status()
    server, key = destination()
    if verbose:
        print(f"  broadcasting : {st.output_active}")
        print(f"  destination  : {server}")
        print(f"  stream key   : {'set' if key else 'NOT SET — cannot go live'}")
        tot = st.output_total_frames or 0
        sk = st.output_skipped_frames or 0
        if tot:
            print(f"  frames       : {tot} sent, {sk} dropped ({100.0*sk/tot:.1f}%)")
        if st.output_active:
            print(f"  uptime       : {int((st.output_duration or 0)/1000)}s")
    return st.output_active

if action == "status":
    print("Stream status:")
    status()

elif action == "start":
    server, key = destination()
    if not key:
        print("REFUSING: no stream key set in OBS. Nothing to broadcast to.")
        print("Set it in OBS -> Settings -> Stream (via noVNC), then retry.")
        sys.exit(1)
    if status(verbose=False):
        print("Already live. Nothing to do."); sys.exit(0)
    print(f"Going LIVE -> {server}")
    c.start_stream()
    time.sleep(6)
    ok = status()
    if not ok:
        print("\nFailed to go live. Check the destination/key in OBS -> Settings -> Stream.")
        sys.exit(1)
    print("\n  LIVE. Stop with:  bash scripts/stream.sh stop")

elif action == "stop":
    if not status(verbose=False):
        print("Not broadcasting. Nothing to do."); sys.exit(0)
    c.stop_stream()
    time.sleep(3)
    print("Stopped broadcasting.")
    status()

else:
    print(f"Unknown action '{action}'. Use: status | start | stop")
    sys.exit(1)
PY
