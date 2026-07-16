#!/bin/bash
# schedule_live.sh — schedule the daily GO-LIVE (and optional auto-stop) with cron.
#
#   bash scripts/schedule_live.sh 19:30            # go live at 7:30pm, every day
#   bash scripts/schedule_live.sh 19:30 23:00      # live 7:30pm, stop 11pm, daily
#   TZ_ZONE=Asia/Dubai bash scripts/schedule_live.sh 19:30    # pick the timezone
#
# Times are in TZ_ZONE (default Asia/Dubai = Dubai time), NOT UTC — cron converts
# for you, so just type your local clock time.
#
#   Show current schedule:   bash scripts/schedule_live.sh
#   Cancel the schedule:     crontab -r
#   Watch it fire:           tail -f /tmp/schedule.log
#
# IMPORTANT: the Vast INSTANCE must be RUNNING (not Stopped) at the scheduled
# time — cron cannot fire on a powered-off box. Leave the instance running, or
# Start it before your go-live time.
set -u
cd "$(dirname "$0")/.."
ROOT=$(pwd)
TZ_ZONE="${TZ_ZONE:-Asia/Dubai}"

START="${1:-}"
STOP="${2:-}"

if [ -z "$START" ]; then
    echo "Current schedule:"
    crontab -l 2>/dev/null | grep -E "CRON_TZ|go_live|go_offline" || echo "  (none set)"
    echo ""
    echo "Set one with:  bash scripts/schedule_live.sh <start HH:MM> [stop HH:MM]"
    exit 0
fi

# HH:MM -> fields (strip a leading zero so 07 -> 7, cron accepts either anyway)
sh=${START%%:*}; sm=${START##*:}
LINES="CRON_TZ=$TZ_ZONE
$sm $sh * * * bash $ROOT/scripts/go_live.sh >> /tmp/schedule.log 2>&1"
if [ -n "$STOP" ]; then
    eh=${STOP%%:*}; em=${STOP##*:}
    LINES="$LINES
$em $eh * * * bash $ROOT/scripts/go_offline.sh >> /tmp/schedule.log 2>&1"
fi

# Replace the crontab with our schedule (this box is dedicated to Bello).
echo "$LINES" | crontab -

echo "Scheduled (timezone: $TZ_ZONE):"
crontab -l | grep -E "CRON_TZ|go_live|go_offline"
echo ""
echo "  -> LIVE at $START${STOP:+ , STOP at $STOP}, every day."
echo "  Watch:   tail -f /tmp/schedule.log"
echo "  Cancel:  crontab -r"
echo ""
echo "  Reminder: the Vast instance must be RUNNING at $START or nothing fires."
