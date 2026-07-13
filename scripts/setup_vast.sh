#!/bin/bash
# setup_vast.sh — one-time setup for a headless Vast.ai GPU instance (Ubuntu).
#
# Installs system packages (ffmpeg, Xvfb, OBS, PulseAudio), starts a virtual
# display + a virtual audio sink, and installs the Python deps. Run ONCE, as
# root, from the repo root:
#
#     bash scripts/setup_vast.sh
#
# After this, start OBS with scripts/start_obs_headless.sh, then build the scene
# with scripts/setup_obs_scene.py.
set -e

echo "[setup_vast] installing system packages..."
apt-get update -qq
# software-properties-common lets us add the official OBS PPA (Ubuntu's own
# obs-studio can be stale / missing the WebSocket server).
apt-get install -y -qq software-properties-common
add-apt-repository -y ppa:obsproject/obs-studio || true
apt-get update -qq
# mesa-utils + libgl1-mesa-dri give OBS a SOFTWARE OpenGL renderer (llvmpipe).
# Without them OBS has no usable GL on a headless compute GPU (Xvfb provides no
# hardware GLX) and segfaults on startup. start_obs_headless.sh forces llvmpipe
# via LIBGL_ALWAYS_SOFTWARE=1; these are the libraries that makes possible.
apt-get install -y -qq ffmpeg xvfb obs-studio pulseaudio pulseaudio-utils espeak-ng \
    libportaudio2 libasound2-plugins tmux git \
    mesa-utils libgl1-mesa-dri libglx-mesa0 libegl1

echo "[setup_vast] starting PulseAudio + null sink (OBS captures its monitor)..."
pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
pactl load-module module-null-sink sink_name=bella_audio \
    sink_properties=device.description="BellaAudio" 2>/dev/null || true
# Make the null sink the default so TTS (output_device: null) lands in it.
pactl set-default-sink bella_audio 2>/dev/null || true

# Route ALSA's default device to PulseAudio. Without this, sounddevice/PortAudio
# (output_device: null) writes to a dead ALSA device and Bella's voice never
# reaches the bella_audio sink -> OBS records silence.
cat > /etc/asound.conf <<'EOF'
pcm.!default { type pulse }
ctl.!default { type pulse }
EOF

echo "[setup_vast] starting virtual display :99..."
if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
    Xvfb :99 -screen 0 1920x1080x24 &
    sleep 2
fi
export DISPLAY=:99
# OBS composites with OpenGL. A headless compute GPU + Xvfb = no hardware GLX, so
# force Mesa's software renderer or OBS segfaults trying to init a GL context.
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

echo "[setup_vast] checking OpenGL (must say llvmpipe, not an NVIDIA card)..."
glxinfo -B 2>/dev/null | grep -iE "OpenGL renderer|OpenGL version" \
    || echo "  [warn] glxinfo failed — OBS will likely crash. Is mesa-utils installed?"

echo "[setup_vast] installing Python dependencies..."
pip install --upgrade pip
# Chatterbox needs a CUDA build of torch; install the project requirements which
# pull in chatterbox-tts, obsws-python, TikTokLive, google-genai, etc.
pip install -r requirements.txt
# Belt-and-braces for the exact deps this deployment relies on:
pip install chatterbox-tts obsws-python TikTokLive google-genai pyyaml \
    python-dotenv sounddevice soundfile numpy edge-tts
# chatterbox-tts pins torch==2.6.0; realign torchvision to match so its compiled
# ops register. A mismatch throws "operator torchvision::nms does not exist",
# which cascades into a transformers "Could not import LlamaModel" error.
pip install torchvision==0.21.0

echo ""
echo "[setup_vast] DONE."
echo "  Next: put your voice-clone refs in  voice_samples/bella_ref_en.wav / bella_ref_ar.wav"
echo "        put your avatar/background clips in  clips/avatar_idle_loops, clips/avatar_talk_loops, clips/backgrounds"
echo "        then:  bash scripts/start_obs_headless.sh"
echo "               DISPLAY=:99 python scripts/setup_obs_scene.py"
echo "               python src/main.py"
