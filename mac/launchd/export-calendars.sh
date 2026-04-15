#!/bin/bash
# Exports Apple Calendar events to /tmp/cal_*.txt files for knowledge base build.
# Uses icalBuddy (brew install ical-buddy) — no Calendar.app dependency,
# properly expands recurring events, uses stable calendar UIDs.

LOG="/Users/eoin/.local/bin/export-calendars.log"
echo "$(date): Calendar export starting..." >> "$LOG"

ICALBUDDY=$(which icalBuddy 2>/dev/null || echo /opt/homebrew/bin/icalBuddy)
if [ ! -x "$ICALBUDDY" ]; then
    echo "$(date): ERROR — icalBuddy not found. Install: brew install ical-buddy" >> "$LOG"
    exit 1
fi

DATE_FROM="2025-01-01"
DATE_TO="2027-06-01"
FAILED=0

export_calendar() {
    local CAL_UID="$1"
    local OUTFILE="$2"
    local LABEL="$3"

    "$ICALBUDDY" \
        -ic "$CAL_UID" \
        -iep "title,datetime,attendees" \
        -po "title,datetime,attendees" \
        -ps "|\n|" \
        -b "ITEM_START\n" \
        -nc -nrd \
        -nnr " " \
        -df "%A %d %B %Y" \
        -tf "%H:%M:%S" \
        -eed \
        eventsFrom:"$DATE_FROM" to:"$DATE_TO" 2>/dev/null | \
    awk '
    /^ITEM_START$/ {
        if (title != "") {
            print "TITLE: " title
            print "START: " start
            if (attendees != "") print "ATTENDEES: " attendees
            print "---"
        }
        title = ""; start = ""; attendees = ""
        next
    }
    /^attendees: / {
        att = substr($0, 12)
        gsub(/, /, "|", att)
        attendees = att
        next
    }
    title == "" { title = $0; next }
    start == "" && / at / { start = $0; next }
    END {
        if (title != "") {
            print "TITLE: " title
            print "START: " start
            if (attendees != "") print "ATTENDEES: " attendees
            print "---"
        }
    }
    ' > "$OUTFILE"

    local COUNT
    COUNT=$(grep -c "^TITLE:" "$OUTFILE" 2>/dev/null) || COUNT=0
    echo "  $LABEL — $COUNT events" >> "$LOG"
    [ "$COUNT" -eq 0 ] && return 1 || return 0
}

# Calendar UID → output file → label
# Get UIDs with: icalBuddy calendars
ERRORS=0
export_calendar "2A36C682-0329-405D-A6DA-55D6D409BC74" "/tmp/cal_eoinlane.txt"      "Eoin Lane"          || ERRORS=$((ERRORS + 1))
export_calendar "4A687BA3-69CF-416A-96C4-58BFECBF8C0D" "/tmp/cal_work.txt"          "Work"               || ERRORS=$((ERRORS + 1))
export_calendar "E0F5B50E-629A-4DC2-A6E5-F07DB88C82A3" "/tmp/calendar_events.txt"   "eoin@novalconsultancy.com" || ERRORS=$((ERRORS + 1))
export_calendar "E6E63F94-588F-4A15-8809-1889C23D6BC4" "/tmp/cal_personal.txt"      "Personal"           || ERRORS=$((ERRORS + 1))
export_calendar "C1BAE074-A8C8-476F-A168-9680D48777BB" "/tmp/cal_home.txt"           "Home"               || ERRORS=$((ERRORS + 1))
export_calendar "2D0FFC84-CE8A-4467-B66D-B4F0655E4956" "/tmp/cal_extra_15.txt"      "Calendar (DCC/ADAPT)" || ERRORS=$((ERRORS + 1))
export_calendar "3DE8357C-DA39-47BA-A8A1-01F1D3B55CF6" "/tmp/cal_nta.txt"           "Calendar (NTA)"     || ERRORS=$((ERRORS + 1))
export_calendar "1A4BFA9C-4D8E-435A-8642-DF97D468939B" "/tmp/cal_adapt.txt"         "ADAPT (Google)"     || ERRORS=$((ERRORS + 1))

if [ "$ERRORS" -gt 0 ]; then
    echo "$(date): Calendar export completed with $ERRORS failures" >> "$LOG"
    exit 1
else
    echo "$(date): Calendar export OK" >> "$LOG"
fi
