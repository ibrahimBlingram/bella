#!/bin/bash
# start_obs_headless.sh — launch OBS on the virtual display :99 (no monitor).
#
# Assumes setup_vast.sh already created the Xvfb :99 display and the PulseAudio
# null sink. Run from the repo root:
#
#     bash scripts/start_obs_headless.sh
#
# OBS starts with the WebSocket server enabled (config it once via the OBS
# profile, or it defaults to :4455). Then build the scene:
#
#     DISPLAY=:99 python scripts/setup_obs_scene.py
export DISPLAY=:99

# Make sure the display exists (setup_vast.sh normally starts it).
if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
    echo "[start_obs] Xvfb :99 not running — starting it..."
    Xvfb :99 -screen 0 1920x1080x24 &
    sleep 2
fi

# Make sure PulseAudio + the null sink are up.
pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
pactl load-module module-null-sink sink_name=bella_audio \
    sink_properties=device.description="BellaAudio" 2>/dev/null || true
pactl set-default-sink bella_audio 2>/dev/null || true

if pgrep -x obs >/dev/null 2>&1; then
    echo "[start_obs] OBS already running on :99"
    exit 0
fi

# --minimize-to-tray keeps it out of the (nonexistent) foreground; the
# WebSocket server + scene run fine headlessly.
obs --minimize-to-tray --disable-shutdown-check 2>/dev/null &
sleep 5
echo "[start_obs] OBS running headlessly on :99 (WebSocket :4455)"
