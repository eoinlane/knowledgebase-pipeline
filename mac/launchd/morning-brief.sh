#!/bin/bash
# Daily morning brief. Runs query_graph.py brief and persists to
# ~/knowledge_base/_briefs/YYYY-MM-DD.md plus a stable ~/morning_brief.md
# pointer to today's file. Triggered by launchd at 06:30; safe to run manually.

LOG="/Users/eoin/.local/bin/morning-brief.log"
BRIEFS_DIR="/Users/eoin/knowledge_base/_briefs"
QUERY_GRAPH="/Users/eoin/query_graph.py"
STABLE="/Users/eoin/morning_brief.md"

mkdir -p "$BRIEFS_DIR"

TODAY=$(date +%Y-%m-%d)
OUT="$BRIEFS_DIR/${TODAY}.md"

echo "$(date): Generating morning brief for $TODAY..." >> "$LOG"

{
    echo "---"
    echo "title: \"Morning Brief $TODAY\""
    echo "date: $TODAY"
    echo "type: morning_brief"
    echo "---"
    echo
    /usr/local/bin/python3 "$QUERY_GRAPH" brief
} > "$OUT" 2>>"$LOG"

# Stable pointer for "the file you read with coffee"
cp "$OUT" "$STABLE"

echo "$(date): Done. $(wc -l < "$OUT") lines → $OUT" >> "$LOG"

# Email the brief. Reads ~/morning_brief.md (the STABLE path written above) and
# sends via Gmail SMTP using an app password stored in the macOS login keychain
# (service=morning-brief-smtp, account=eoinlane@gmail.com).
# Non-fatal: if email fails, the markdown file is still produced and this
# wrapper still exits 0. The launchd agent should not retry on email failure.
EMAILER="/Users/eoin/morning_brief_emailer.py"
if [ -x "$EMAILER" ]; then
    if /usr/local/bin/python3 "$EMAILER" >> "$LOG" 2>&1; then
        echo "$(date): Email sent OK" >> "$LOG"
    else
        echo "$(date): Email FAILED (markdown file still at $STABLE)" >> "$LOG"
    fi
fi

exit 0
