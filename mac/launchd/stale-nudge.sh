#!/bin/bash
# Weekly stale-commitment nudge. Runs query_graph.py stale-nudge and emails
# the result to eoinlane@gmail.com. Triggered by launchd on Friday 06:30;
# safe to run manually any time.
# Companion to morning-brief.sh (daily 06:30) — same SMTP path, different
# command + subject. Targets Eoin's open commitments >3 weeks old.

LOG="/Users/eoin/.local/bin/stale-nudge.log"
NUDGES_DIR="/Users/eoin/knowledge_base/_nudges"
QUERY_GRAPH="/Users/eoin/query_graph.py"
EMAILER="/Users/eoin/morning_brief_emailer.py"
STABLE="/Users/eoin/stale_nudge.md"

mkdir -p "$NUDGES_DIR"

TODAY=$(date +%Y-%m-%d)
OUT="$NUDGES_DIR/${TODAY}.md"

echo "$(date): Generating stale nudge for $TODAY..." >> "$LOG"

{
    echo "---"
    echo "title: \"Stale Nudge $TODAY\""
    echo "date: $TODAY"
    echo "type: stale_nudge"
    echo "---"
    echo
    /usr/local/bin/python3 "$QUERY_GRAPH" stale-nudge
} > "$OUT" 2>>"$LOG"

# Stable pointer
cp "$OUT" "$STABLE"

echo "$(date): Wrote $(wc -l < "$OUT") lines → $OUT" >> "$LOG"

# Email it. Non-fatal — markdown file always produced even if SMTP fails.
if [ -x "$EMAILER" ]; then
    if /usr/local/bin/python3 "$EMAILER" --file "$STABLE" --subject "Stale Commitments" >> "$LOG" 2>&1; then
        echo "$(date): Email sent OK" >> "$LOG"
    else
        echo "$(date): Email FAILED (markdown still at $STABLE)" >> "$LOG"
    fi
fi

exit 0
