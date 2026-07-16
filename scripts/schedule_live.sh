#!/bin/bash
# schedule_live.sh — schedule the daily GO-LIVE (and optional auto-stop) with cron.
#
#   bash scripts/schedule_live.sh 19:30            # go live at 7:30pm, every day
#   bash scripts/schedule_live.sh 19:30 23:00      # live 7:30pm, stop 11pm, daily
#   TZ_ZONE=Asia/Kolkata bash scripts/schedule_live.sh 19:30   # pick the timezone
#
# You type your LOCAL clock time (TZ_ZONE, default Asia/Kolkata = India/IST). The
# script converts it to UTC and writes the cron entry in UTC — because this box's
# cron does NOT honour CRON_TZ (it silently treated local times as UTC, which is
# why a 19:30 schedule never fired at 19:30 local). Converting ourselves is the
# reliable fix.
#
#   Show current schedule:   bash scripts/schedule_live.sh
#   Cancel the schedule:     crontab -r
#   Watch it fire:           tail -f /tmp/schedule.log
#
# IMPORTANT: the Vast INSTANCE must be RUNNING (not Stopped) at the scheduled
# time — cron cannot fire on a powered-off box.
#
# Note: times are converted using TODAY's UTC offset. IST has no daylight saving,
# so it is always correct; for a DST timezone, re-run this after a clock change.
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)
TZ_ZONE="${TZ_ZONE:-Asia/Kolkata}"
PY=/venv/main/bin/python3.12
[ -x "$PY" ] || PY=python3

START="${1:-}"
STOP="${2:-}"

if [ -z "$START" ]; then
    echo "Current schedule:"
    crontab -l 2>/dev/null | grep -E "go_live|go_offline" || echo "  (none set)"
    echo ""
    echo "Set one with:  bash scripts/schedule_live.sh <start HH:MM> [stop HH:MM]"
    exit 0
fi

# Convert "HH:MM" in TZ_ZONE -> "MM HH" (cron's minute-then-hour) in UTC.
to_utc_cron() {
    "$PY" - "$TZ_ZONE" "$1" <<'PY'
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
tz = ZoneInfo(sys.argv[1])
hh, mm = (int(x) for x in sys.argv[2].split(":"))
local = datetime.now(tz).replace(hour=hh, minute=mm, second=0, microsecond=0)
u = local.astimezone(ZoneInfo("UTC"))
print(f"{u.minute} {u.hour}")
PY
}

START_CRON=$(to_utc_cron "$START") || { echo "bad start time '$START'"; exit 1; }
LINES="$START_CRON * * * bash $ROOT/scripts/go_live.sh >> /tmp/schedule.log 2>&1"
if [ -n "$STOP" ]; then
    STOP_CRON=$(to_utc_cron "$STOP") || { echo "bad stop time '$STOP'"; exit 1; }
    LINES="$LINES
$STOP_CRON * * * bash $ROOT/scripts/go_offline.sh >> /tmp/schedule.log 2>&1"
fi

# Replace the crontab with our schedule (this box is dedicated to Bello).
echo "$LINES" | crontab -

echo "Scheduled — you type $TZ_ZONE time, cron runs in UTC:"
echo "  LIVE  at $START $TZ_ZONE  (cron: '$START_CRON * * *' UTC)"
[ -n "$STOP" ] && echo "  STOP  at $STOP $TZ_ZONE  (cron: '$STOP_CRON * * *' UTC)"
echo ""
echo "Installed crontab:"; crontab -l
echo ""
echo "  Watch:   tail -f /tmp/schedule.log"
echo "  Cancel:  crontab -r"
echo "  Reminder: the Vast instance must be RUNNING at the scheduled time."
