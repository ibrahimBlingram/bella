#!/bin/bash
# start_all.sh — bring Bello up from a cold box (Vast.ai, after a stop/start).
#
# A Vast stop/start preserves the FILESYSTEM but kills every PROCESS, so the
# virtual display, the audio sink and OBS are all gone even though the repo, the
# venv and the OBS scene are exactly as you left them. This restarts the lot, in
# the order they depend on each other, and is safe to re-run (each step is a
# no-op if that piece is already up).
#
#     bash scripts/start_all.sh          # bring everything up, then start Bello
#     bash scripts/start_all.sh --no-run # bring the stack up but don't start main.py
#
# Watch him:   tail -f /tmp/bello.log
# Stop him:    pkill -f src/main.py
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)

step() { printf "\n[%s] %s\n" "$1" "$2"; }

# 1. Virtual display. OBS needs an X display even with no monitor.
step 1/5 "virtual display :99"
if pgrep -f "Xvfb :99" >/dev/null; then
    echo "  already running"
else
    setsid Xvfb :99 -screen 0 1920x1080x24 >/tmp/xvfb.log 2>&1 </dev/null &
    sleep 3
    pgrep -f "Xvfb :99" >/dev/null && echo "  started" || { echo "  FAILED"; tail -3 /tmp/xvfb.log; exit 1; }
fi
export DISPLAY=:99
# OBS composites with OpenGL; a headless compute GPU gives no hardware GLX, so
# use Mesa's software renderer. (Needs libgl1-mesa-dri — setup_vast.sh installs it.)
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

# 2. Audio. TTS writes to the default sink (tts.output_device: null); OBS
#    captures that sink's MONITOR. No sink -> OBS records silence.
step 2/5 "PulseAudio + bella_audio null sink"
pulseaudio --check 2>/dev/null || pulseaudio --start --exit-idle-time=-1 2>/dev/null
sleep 1
pactl list short sinks 2>/dev/null | grep -q bella_audio \
    || pactl load-module module-null-sink sink_name=bella_audio \
         sink_properties=device.description=BellaAudio >/dev/null 2>&1
pactl set-default-sink bella_audio >/dev/null 2>&1
pactl list short sinks 2>/dev/null | grep -q bella_audio \
    && echo "  bella_audio ready (default sink)" || { echo "  FAILED — no audio sink"; exit 1; }

# 3. OBS. The scene, sources and stream settings persist in ~/.config/obs-studio,
#    so this comes back exactly as it was — no need to rebuild the scene.
step 3/5 "OBS (headless, WebSocket :4455)"
if pgrep -x obs >/dev/null; then
    echo "  already running"
else
    obs --minimize-to-tray --disable-shutdown-check \
        --profile Bella --collection Bella >/tmp/obs.log 2>&1 &
    sleep 8
    pgrep -x obs >/dev/null && echo "  started" || { echo "  FAILED"; tail -5 /tmp/obs.log; exit 1; }
fi

# 3b. VNC, so you can actually SEE and click the OBS window on a headless box.
#     Bound to LOOPBACK ONLY — reachable solely through an SSH tunnel, never
#     exposed on the public internet. From your laptop:
#         ssh -f -N -L 5901:localhost:5900 -p <port> root@<host>
#         open vnc://localhost:5901          # macOS; any VNC client works
#     (5901 locally because macOS already runs its own Screen Sharing on 5900.)
step 3b "x11vnc (view OBS remotely)"
if ! command -v x11vnc >/dev/null; then
    echo "  x11vnc not installed — skipping (apt-get install -y x11vnc to enable)"
elif pgrep -x x11vnc >/dev/null; then
    echo "  already running"
else
    setsid x11vnc -display :99 -forever -shared -localhost -rfbport 5900 -nopw \
        >/tmp/x11vnc.log 2>&1 </dev/null &
    sleep 2
    pgrep -x x11vnc >/dev/null && echo "  started (loopback :5900 — tunnel in to view)" \
        || echo "  failed to start (non-fatal; Bello runs fine without it)"
fi

# 4. Python env (Vast keeps it at /venv/main, not a repo-local .venv).
step 4/5 "python env"
source /venv/main/bin/activate
echo "  $(python -V), torch $(python -c 'import torch;print(torch.__version__)' 2>/dev/null)"
python -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' \
    && echo "  CUDA available -> Chatterbox will use the GPU" \
    || echo "  WARNING: no CUDA — Chatterbox can't load, voice falls back to edge-tts"

# 5. Bello.
step 5/5 "Bello"
if [ "${1:-}" = "--no-run" ]; then
    echo "  --no-run: stack is up, main.py NOT started."
    echo "  Start him with:  python -u src/main.py"
    exit 0
fi
pkill -f "src/main.py" 2>/dev/null && sleep 2
setsid nohup python -u src/main.py >/tmp/bello.log 2>&1 </dev/null &
sleep 5
if pgrep -f "src/main.py" >/dev/null; then
    echo "  started (loading both Chatterbox models takes ~60s on first run)"
    echo ""
    echo "  watch:  tail -f /tmp/bello.log"
    echo "  stop :  pkill -f src/main.py"
else
    echo "  FAILED to start:"; tail -10 /tmp/bello.log
    exit 1
fi
