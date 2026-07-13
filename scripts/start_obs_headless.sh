#!/bin/bash
# start_obs_headless.sh — launch OBS on the virtual display :99 (no monitor).
#
# A fresh OBS won't enable the WebSocket server and shows a first-run wizard —
# both fatal with no screen. So this seeds OBS's config ONCE (idempotent: it
# never overwrites an existing config, so the scene you build later survives):
#   * WebSocket enabled on :4455, no auth  (matches config.yaml obs.password "")
#   * skip the first-run wizard
#   * a "Bella" profile: vertical 1080x1920 TikTok canvas, 30 fps, NVENC @ 6 Mbps
#
# Run from the repo root:
#     bash scripts/start_obs_headless.sh
# Then build the scene:
#     DISPLAY=:99 python scripts/setup_obs_scene.py
export DISPLAY=:99
OBS_CFG="$HOME/.config/obs-studio"

# --- WHY OBS CRASHES ON A VAST.AI BOX, AND WHAT FIXES IT ---------------------
# Vast's GPUs are headless COMPUTE cards: no display engine, and Xvfb gives no
# hardware GLX. OBS composites with OpenGL, so on startup it finds the NVIDIA
# libGL, tries to create a hardware GL context against a display that cannot
# provide one, and segfaults.
#
# Forcing Mesa's software renderer (llvmpipe) makes OBS create a CPU GL context
# instead of dying. This is the fix for the crash — it is not optional here.
#
# Note what this does NOT give up: NVENC (encode) and NVDEC (decode) do not need
# a display and keep running ON THE GPU. Only COMPOSITING lands on the CPU, and
# they are separate hardware blocks from the CUDA cores Chatterbox uses — so the
# GPU stays effectively dedicated to Chatterbox either way.
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
# llvmpipe is a CPU rasteriser: every extra pixel costs CPU. Keep the canvas
# modest (720x1280 @ 20fps, set in the profile below) or the compositor drops
# frames — that is what commit 048be83 was fixing.

# --- make sure the virtual display + audio sink are up ---
if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
    echo "[start_obs] Xvfb :99 not running — starting it..."
    Xvfb :99 -screen 0 1920x1080x24 &
    sleep 2
fi
pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
pactl load-module module-null-sink sink_name=bella_audio \
    sink_properties=device.description="BellaAudio" 2>/dev/null || true
pactl set-default-sink bella_audio 2>/dev/null || true

# --- seed OBS config once (never clobber an existing setup) ---
if [ ! -d "$OBS_CFG" ]; then
    echo "[start_obs] seeding OBS config (WebSocket + Bella profile)..."
    mkdir -p "$OBS_CFG/plugin_config/obs-websocket"
    mkdir -p "$OBS_CFG/basic/profiles/Bella"
    mkdir -p "$OBS_CFG/basic/scenes"

    # WebSocket: enabled, no auth, port 4455.
    cat > "$OBS_CFG/plugin_config/obs-websocket/config.json" <<'JSON'
{
    "alerts_enabled": false,
    "auth_required": false,
    "first_load": false,
    "server_enabled": true,
    "server_password": "",
    "server_port": 4455
}
JSON

    # Global: skip the first-run wizard, select the Bella profile.
    cat > "$OBS_CFG/global.ini" <<'INI'
[General]
FirstRun=false
LastVersion=503316480

[Basic]
Profile=Bella
ProfileDir=Bella
SceneCollection=Bella
SceneCollectionFile=Bella
INI

    # Encoder: NVENC when the box really has it, else x264. NVENC is a dedicated
    # block on the GPU die — it does NOT compete with Chatterbox for CUDA cores,
    # and it works on a headless card. x264 would push a 720x1280 encode onto the
    # same CPU that is already doing all the compositing, which is how you get a
    # stuttering avatar and dropped frames.
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
        ENCODER=jim_nvenc
        echo "[start_obs] NVIDIA GPU detected -> NVENC encoding (GPU, off the CPU)"
    else
        ENCODER=x264
        echo "[start_obs] no NVIDIA GPU -> x264 encoding (CPU)"
    fi

    # Canvas stays 720x1280 @ 20fps: the compositor is llvmpipe (CPU), so pixels
    # are expensive. 720x1280 is still correct 9:16 vertical for TikTok.
    cat > "$OBS_CFG/basic/profiles/Bella/basic.ini" <<INI
[General]
Name=Bella

[Video]
BaseCX=720
BaseCY=1280
OutputCX=720
OutputCY=1280
FPSType=1
FPSCommon=20

[Output]
Mode=Simple

[SimpleOutput]
VBitrate=6000
StreamEncoder=$ENCODER
ABitrate=160

[Audio]
SampleRate=48000
ChannelSetup=Stereo
INI

    # Empty scene collection — setup_obs_scene.py builds the "Live" scene over
    # the WebSocket and OBS persists it here.
    cat > "$OBS_CFG/basic/scenes/Bella.json" <<'JSON'
{
    "current_scene": "",
    "current_program_scene": "",
    "name": "Bella",
    "scene_order": [],
    "sources": [],
    "groups": []
}
JSON
else
    echo "[start_obs] existing OBS config found — leaving it untouched."
fi

# --- launch OBS ---
if pgrep -x obs >/dev/null 2>&1; then
    echo "[start_obs] OBS already running on :99"
    exit 0
fi
obs --minimize-to-tray --disable-shutdown-check \
    --profile Bella --collection Bella 2>/dev/null &
sleep 6
if pgrep -x obs >/dev/null 2>&1; then
    echo "[start_obs] OBS running headlessly on :99 (WebSocket :4455, no auth)"
else
    echo "[start_obs] WARNING: OBS did not stay up — check 'DISPLAY=:99 obs' output."
fi
