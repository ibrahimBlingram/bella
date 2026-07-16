#!/bin/bash
# go_live.sh — make the stream LIVE from whatever state the box is in.
#
#   1) bring the stack (display, audio, OBS) up if it is down   (idempotent)
#   2) make sure Bello (src/main.py) is running                 (start only if down)
#   3) start the RTMP broadcast to Restream/TikTok
#
# Safe to run any time. This is the target of the cron schedule (schedule_live.sh),
# but you can also run it by hand:  bash scripts/go_live.sh
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)

echo "[go_live] $(date '+%Y-%m-%d %H:%M:%S %Z') — ensuring the stack is up"
bash scripts/start_all.sh --no-run || { echo "[go_live] stack failed to come up"; exit 1; }

source /venv/main/bin/activate 2>/dev/null

# Start Bello ONLY if he is not already running — never restart a healthy one
# (a restart reloads both voice models, ~60s of silence for no reason).
if pgrep -f "src/main.py" >/dev/null; then
    echo "[go_live] Bello already running"
else
    echo "[go_live] starting Bello (loading voice models takes ~60s)"
    : > /tmp/bello.log
    setsid nohup python -u src/main.py >/tmp/bello.log 2>&1 </dev/null &
    for i in $(seq 1 40); do
        grep -qa "Bello LIVE" /tmp/bello.log 2>/dev/null && break
        sleep 3
    done
    grep -qa "Bello LIVE" /tmp/bello.log 2>/dev/null \
        && echo "[go_live] Bello up" \
        || echo "[go_live] WARNING: Bello not confirmed up — starting the broadcast anyway"
fi

echo "[go_live] starting the broadcast"
bash scripts/stream.sh start
