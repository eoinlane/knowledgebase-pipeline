#!/bin/bash
# Exports Apple Calendar events to /tmp/cal_*.txt files for knowledge base build.
# Launches Calendar if not running, runs the AppleScript, then quits Calendar.

LOG="/Users/eoin/.local/bin/export-calendars.log"
echo "$(date): Calendar export starting..." >> "$LOG"

# Ensure Calendar is running and fully initialized
if ! pgrep -x "Calendar" > /dev/null; then
    echo "  Launching Calendar..." >> "$LOG"
    open -a Calendar
    sleep 15
else
    sleep 3
fi

# Wait until Calendar responds to a basic query (up to 30s)
for i in $(seq 1 6); do
    if osascript -e 'tell application "Calendar" to get name of first calendar' > /dev/null 2>&1; then
        break
    fi
    echo "  Waiting for Calendar to be ready (attempt $i)..." >> "$LOG"
    sleep 5
done

# Clear previous log
rm -f /tmp/cal_export_log.txt

# Run the export — retry once on failure
osascript /Users/eoin/.local/bin/export-calendars.applescript >> "$LOG" 2>&1
EXIT=$?

if [ $EXIT -ne 0 ]; then
    echo "  Export failed, retrying after 20s..." >> "$LOG"
    sleep 20
    rm -f /tmp/cal_export_log.txt
    osascript /Users/eoin/.local/bin/export-calendars.applescript >> "$LOG" 2>&1
    EXIT=$?
fi

# Append per-calendar results to log
if [ -f /tmp/cal_export_log.txt ]; then
    cat /tmp/cal_export_log.txt >> "$LOG"
fi

if [ $EXIT -eq 0 ]; then
    echo "$(date): Calendar export OK" >> "$LOG"
    for f in /tmp/cal_eoinlane.txt /tmp/cal_work.txt /tmp/calendar_events.txt \
              /tmp/cal_extra_15.txt /tmp/cal_nta.txt /tmp/cal_personal.txt /tmp/cal_home.txt; do
        if [ -f "$f" ]; then
            count=$(grep -c "^TITLE:" "$f" 2>/dev/null || echo 0)
            echo "  $f — $count events" >> "$LOG"
        fi
    done
else
    echo "$(date): Calendar export FAILED (exit $EXIT)" >> "$LOG"
    exit 1
fi
