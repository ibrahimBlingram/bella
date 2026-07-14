#!/bin/bash
# stop.sh — ONE command. Stops everything run.sh started.
#
#     cd /workspace/bella && bash stop.sh          # stop Bello + OBS + everything
#     cd /workspace/bella && bash stop.sh bello    # stop ONLY Bello, leave OBS up
#
# Ctrl-C does NOT work on these: run.sh starts them with setsid, so they are
# detached from your terminal and survive it closing (that's the point — Bello
# keeps streaming after you disconnect). They have to be killed by name.
#
# Order matters. The broadcast is stopped FIRST, so we never yank OBS out from
# under a live stream.
set -u
cd "$(dirname "$0")"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
info() { printf "  · %s\n" "$1"; }

# ---------------------------------------------------------- 1. stop the stream
# Do this before anything else — killing OBS mid-broadcast is an unclean cut.
if pgrep -x obs >/dev/null && (exec 3<>/dev/tcp/localhost/4455) 2>/dev/null; then
    source /venv/main/bin/activate 2>/dev/null
    python - <<'PY' 2>/dev/null
import yaml, time, obsws_python as obs
cfg = yaml.safe_load(open("config.yaml")); o = cfg["obs"]
try:
    c = obs.ReqClient(host=o["host"], port=o["port"], password=o["password"])
    if c.get_stream_status().output_active:
        c.stop_stream(); time.sleep(2)
        print("  \033[32m✓\033[0m broadcast stopped")
    else:
        print("  · was not broadcasting")
except Exception:
    pass
PY
fi

# ------------------------------------------------------------------- 2. Bello
if pgrep -f "src/main.py" >/dev/null; then
    pkill -f "src/main.py"; sleep 2
    pgrep -f "src/main.py" >/dev/null && { pkill -9 -f "src/main.py"; sleep 1; }
    ok "Bello stopped (GPU freed)"
else
    info "Bello was not running"
fi

# "bello" = stop only Bello, keep OBS/display/audio up so you can restart him fast.
if [ "${1:-all}" = "bello" ]; then
    echo ""
    echo "OBS and the display are still up. Restart Bello with:"
    echo "    source /venv/main/bin/activate && python -u src/main.py"
    exit 0
fi

# --------------------------------------------------------------------- 3. OBS
if pgrep -x obs >/dev/null; then
    # SIGTERM first: a clean OBS exit removes its own .sentinel marker. A SIGKILL
    # leaves the marker behind, and enough of those make the NEXT start hang on a
    # "run in safe mode?" dialog nobody can click on a headless box.
    pkill -x obs
    for i in $(seq 1 10); do
        sleep 1
        pgrep -x obs >/dev/null || break
    done
    if pgrep -x obs >/dev/null; then
        pkill -9 -x obs; sleep 1
        rm -rf "$HOME/.config/obs-studio/.sentinel"   # forced kill -> clear it ourselves
        ok "OBS force-stopped (crash markers cleared)"
    else
        ok "OBS stopped cleanly"
    fi
else
    info "OBS was not running"
fi

# ------------------------------------------------- 4. viewer, display, audio
pkill -f websockify >/dev/null 2>&1 && ok "noVNC stopped"     || info "noVNC was not running"
pkill -x x11vnc     >/dev/null 2>&1 && ok "x11vnc stopped"    || info "x11vnc was not running"
pkill -f "Xvfb :99" >/dev/null 2>&1 && ok "display stopped"   || info "display was not running"
pulseaudio --kill   >/dev/null 2>&1 && ok "audio stopped"     || info "audio was not running"

echo ""
echo "Everything stopped. Start again with:  bash run.sh"
echo ""
echo "NOTE: the Vast instance is still BILLING (~\$0.089/hr) even with"
echo "      nothing running. Hit STOP on the Vast dashboard to pause billing."
echo "      (Stop is safe — everything comes back. Never Destroy/Recycle.)"
