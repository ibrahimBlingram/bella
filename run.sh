#!/bin/bash
# run.sh — ONE command. Starts everything, including OBS in your browser.
#
#     cd /workspace/bella && bash run.sh
#
# That's it. No SSH, no tunnels, no other commands. Run it from the Jupyter
# terminal. It brings up every piece, prints the browser link for OBS, and starts
# Bello. Safe to re-run: anything already running is left alone.
#
# It does NOT go live. Broadcasting is deliberate and separate:
#     bash scripts/stream.sh start     # go live
#     bash scripts/stream.sh stop      # stop broadcasting
set -u
cd "$(dirname "$0")"

VNC_PASS="bello388"
NOVNC_PORT=16006          # Caddy proxies external 6006 -> 16006 -> this
EXTERNAL=6006

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }
step() { printf "\n\033[1m%s\033[0m\n" "$1"; }

port_open() { (exec 3<>/dev/tcp/localhost/"$1") 2>/dev/null; }

# ---------------------------------------------------------------- 1. display
step "1. Virtual display"
# OBS is a GUI app: it will not launch without a screen, even with nobody watching.
if pgrep -f "Xvfb :99" >/dev/null; then
    ok "already running"
else
    setsid Xvfb :99 -screen 0 1920x1080x24 >/tmp/xvfb.log 2>&1 </dev/null &
    sleep 3
    pgrep -f "Xvfb :99" >/dev/null && ok "started" || { bad "FAILED"; tail -3 /tmp/xvfb.log; exit 1; }
fi
export DISPLAY=:99
# A headless compute GPU gives no hardware OpenGL, so OBS must use Mesa's software
# renderer or it segfaults on startup.
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

# ------------------------------------------------------------------ 2. audio
step "2. Audio"
# The server has NO sound card. This is a fake speaker: Bello's voice is played
# into it, and OBS records from it. Without it the stream is SILENT.
pulseaudio --check 2>/dev/null || pulseaudio --start --exit-idle-time=-1 2>/dev/null
sleep 1
pactl list short sinks 2>/dev/null | grep -q bella_audio \
    || pactl load-module module-null-sink sink_name=bella_audio \
         sink_properties=device.description=BellaAudio >/dev/null 2>&1
pactl set-default-sink bella_audio >/dev/null 2>&1
pactl list short sinks 2>/dev/null | grep -q bella_audio \
    && ok "bella_audio sink ready" || { bad "no audio sink — stream would be silent"; exit 1; }

# -------------------------------------------------------------------- 3. OBS
step "3. OBS"
if pgrep -x obs >/dev/null && port_open 4455; then
    ok "already running"
else
    pkill -9 -x obs 2>/dev/null && sleep 2
    # OBS leaves a marker in .sentinel on every non-clean exit (we always pkill it).
    # Enough markers and OBS decides it crashed, then blocks forever on a "safe
    # mode?" dialog nobody can click on a headless box — process alive, websocket
    # never opens. Clearing them is the fix; --disable-shutdown-check is not.
    rm -rf "$HOME/.config/obs-studio/.sentinel"
    setsid obs --minimize-to-tray --disable-shutdown-check --disable-missing-files-check \
        --profile Bella --collection Bella >/tmp/obs.log 2>&1 </dev/null &
    for i in $(seq 1 20); do
        sleep 2
        port_open 4455 && break
    done
    port_open 4455 && ok "started (websocket :4455)" \
        || { bad "OBS is up but its websocket never opened:"; tail -5 /tmp/obs.log; exit 1; }
fi

# ------------------------------------------------------- 4. OBS in the browser
step "4. OBS in your browser"
# x11vnc reads the fake screen; noVNC turns that into a web page; Caddy (already
# running on this box) serves it with token auth on an external port.
if ! command -v x11vnc >/dev/null || ! command -v websockify >/dev/null; then
    bad "x11vnc/websockify missing — run:  apt-get install -y x11vnc novnc websockify"
else
    if ! pgrep -x x11vnc >/dev/null; then
        mkdir -p "$HOME/.vnc"
        x11vnc -storepasswd "$VNC_PASS" "$HOME/.vnc/passwd" >/dev/null 2>&1
        setsid x11vnc -display :99 -forever -shared -localhost -rfbport 5900 \
            -rfbauth "$HOME/.vnc/passwd" >/tmp/x11vnc.log 2>&1 </dev/null &
        sleep 2
    fi
    if ! pgrep -f "websockify.*$NOVNC_PORT" >/dev/null; then
        # Tensorboard squats on this port in the stock image and we don't use it.
        supervisorctl stop tensorboard >/dev/null 2>&1
        sleep 1
        setsid websockify --web=/usr/share/novnc "$NOVNC_PORT" localhost:5900 \
            >/tmp/novnc.log 2>&1 </dev/null &
        sleep 3
    fi
    pgrep -f "websockify.*$NOVNC_PORT" >/dev/null && ok "noVNC ready" || bad "noVNC failed (see /tmp/novnc.log)"
fi

# ------------------------------------------------------------------- 5. Bello
step "5. Bello"
source /venv/main/bin/activate 2>/dev/null
python -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' 2>/dev/null \
    && ok "GPU available — Chatterbox will use it" \
    || bad "no CUDA — voice will fall back to edge-tts"
pkill -f "src/main.py" 2>/dev/null && sleep 2
setsid nohup python -u src/main.py >/tmp/bello.log 2>&1 </dev/null &
for i in $(seq 1 30); do
    sleep 3
    grep -q "Bello LIVE" /tmp/bello.log 2>/dev/null && break
    grep -q "Traceback" /tmp/bello.log 2>/dev/null && { bad "Bello crashed:"; tail -8 /tmp/bello.log; exit 1; }
done
grep -q "Bello LIVE" /tmp/bello.log 2>/dev/null \
    && ok "Bello is running (both Chatterbox models on the GPU)" \
    || { bad "Bello didn't come up — check: tail -30 /tmp/bello.log"; tail -5 /tmp/bello.log; }

# ------------------------------------------------------------------ where now
IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "<instance-ip>")
PORT_VAR="VAST_TCP_PORT_${EXTERNAL}"
HOSTPORT="${!PORT_VAR:-<mapped-port>}"
TOKEN="${OPEN_BUTTON_TOKEN:-}"

cat <<EOF

────────────────────────────────────────────────────────────────
 EVERYTHING IS RUNNING.

 See OBS in your browser:
   http://${IP}:${HOSTPORT}/vnc.html?token=${TOKEN}
   → click Connect → password: ${VNC_PASS}

 (If your network blocks that, run this on YOUR laptop instead:
    ssh -f -N -L 8081:localhost:${NOVNC_PORT} -p <ssh-port> root@${IP}
  then open http://localhost:8081/vnc.html )

 Bello is talking into OBS, but NOT broadcasting yet.

   bash scripts/stream.sh start     ← GO LIVE
   bash scripts/stream.sh stop      ← stop broadcasting
   bash scripts/stream.sh status    ← live? dropped frames?
   bash scripts/preview.sh          ← what's on screen right now
   tail -f /tmp/bello.log           ← watch him think
────────────────────────────────────────────────────────────────
EOF
