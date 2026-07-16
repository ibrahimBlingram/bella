#!/bin/bash
# go_offline.sh — stop the broadcast at a scheduled time.
#
# This ONLY stops sending frames out. Bello (src/main.py) and OBS keep running,
# so the next go_live.sh just starts the broadcast again — no full restart.
#
#   bash scripts/go_offline.sh
set -u
cd "$(dirname "$0")/.."
echo "[go_offline] $(date '+%Y-%m-%d %H:%M:%S %Z') — stopping the broadcast"
bash scripts/stream.sh stop
