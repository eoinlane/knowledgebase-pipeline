#!/bin/bash
# Exports Apple Calendar events to ~/.local/share/kb/calendars/cal_*.txt for
# the knowledge base build. Uses icalBuddy (brew install ical-buddy) — no
# Calendar.app dependency, properly expands recurring events, uses stable
# calendar UIDs.
#
# Output path moved from /tmp/ → ~/.local/share/kb/calendars/ on 2026-04-27
# because macOS clears /tmp on reboot, which silently dropped calendar data
# from any post-reboot CSV-driven sync until the next 4am rebuild.

LOG="/Users/eoin/.local/bin/export-calendars.log"
CAL_DIR="$HOME/.local/share/kb/calendars"
mkdir -p "$CAL_DIR"
echo "$(date): Calendar export starting (to $CAL_DIR)..." >> "$LOG"

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

    # Note: -eed removed so the datetime line includes end time as
    # "Monday 27 April 2026 at 16:00:00 - 17:00:00". The matcher needs end
    # time to handle recordings that start late in a long meeting (e.g. board
    # meetings that overrun, or where Eoin starts capture mid-discussion).
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
        eventsFrom:"$DATE_FROM" to:"$DATE_TO" 2>/dev/null | \
    awk '
    /^ITEM_START$/ {
        if (title != "") {
            print "TITLE: " title
            print "START: " start
            if (end_time != "") print "END: " end_time
            if (attendees != "") print "ATTENDEES: " attendees
            print "---"
        }
        title = ""; start = ""; end_time = ""; attendees = ""
        next
    }
    /^attendees: / {
        att = substr($0, 12)
        # Comma is the delimiter between attendees, EXCEPT when Outlook
        # exports a name in last-first form ("Dooley, Alan"). Detect this:
        # if a single-word token is followed by another single-word token
        # (neither contains space or @), treat them as last,first and
        # recombine as "First Last". Multi-word tokens (e.g. "Stephen
        # Rigney") and emails are passed through unchanged.
        n = split(att, parts, /, /)
        out = ""
        i = 1
        while (i <= n) {
            cur = parts[i]
            sub(/^ +/, "", cur); sub(/ +$/, "", cur)
            if (i < n) {
                nxt = parts[i+1]
                sub(/^ +/, "", nxt); sub(/ +$/, "", nxt)
                if (cur !~ /[ @]/ && nxt !~ /[ @]/ && cur != "" && nxt != "") {
                    cur = nxt " " cur
                    i++
                }
            }
            out = (out == "" ? cur : out "|" cur)
            i++
        }
        attendees = out
        next
    }
    title == "" { title = $0; next }
    start == "" && / at / {
        # Datetime line may be "Day DD Month YYYY at HH:MM:SS" (no end) OR
        # "Day DD Month YYYY at HH:MM:SS - HH:MM:SS" (same-day end) OR
        # "Day DD Month YYYY at HH:MM:SS - Day DD Month YYYY at HH:MM:SS"
        # (multi-day end). Capture both pieces.
        line = $0
        sep = " - "
        sep_pos = index(line, sep)
        if (sep_pos > 0) {
            start = substr(line, 1, sep_pos - 1)
            end_time = substr(line, sep_pos + length(sep))
        } else {
            start = line
        }
        next
    }
    END {
        if (title != "") {
            print "TITLE: " title
            print "START: " start
            if (end_time != "") print "END: " end_time
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
export_calendar "2A36C682-0329-405D-A6DA-55D6D409BC74" "$CAL_DIR/cal_eoinlane.txt"      "Eoin Lane"          || ERRORS=$((ERRORS + 1))
export_calendar "4A687BA3-69CF-416A-96C4-58BFECBF8C0D" "$CAL_DIR/cal_work.txt"          "Work"               || ERRORS=$((ERRORS + 1))
export_calendar "E0F5B50E-629A-4DC2-A6E5-F07DB88C82A3" "$CAL_DIR/calendar_events.txt"   "eoin@novalconsultancy.com" || ERRORS=$((ERRORS + 1))
export_calendar "E6E63F94-588F-4A15-8809-1889C23D6BC4" "$CAL_DIR/cal_personal.txt"      "Personal"           || ERRORS=$((ERRORS + 1))
export_calendar "C1BAE074-A8C8-476F-A168-9680D48777BB" "$CAL_DIR/cal_home.txt"          "Home"               || ERRORS=$((ERRORS + 1))
export_calendar "2D0FFC84-CE8A-4467-B66D-B4F0655E4956" "$CAL_DIR/cal_extra_15.txt"      "Calendar (DCC/ADAPT)" || ERRORS=$((ERRORS + 1))
export_calendar "3DE8357C-DA39-47BA-A8A1-01F1D3B55CF6" "$CAL_DIR/cal_nta.txt"           "Calendar (NTA)"     || ERRORS=$((ERRORS + 1))
export_calendar "1A4BFA9C-4D8E-435A-8642-DF97D468939B" "$CAL_DIR/cal_adapt.txt"         "ADAPT (Google)"     || ERRORS=$((ERRORS + 1))

if [ "$ERRORS" -gt 0 ]; then
    echo "$(date): Calendar export completed with $ERRORS failures" >> "$LOG"
    exit 1
else
    echo "$(date): Calendar export OK" >> "$LOG"
fi
