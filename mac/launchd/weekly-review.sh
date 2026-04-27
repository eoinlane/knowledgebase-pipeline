#!/bin/bash
# Weekly KB review digest. Runs query_graph.py review and persists the
# output to ~/knowledge_base/_reviews/YYYY-Wnn.md so each week's digest
# is always available, browsable, and survives across sessions.
# Triggered by launchd Monday 07:00; safe to run manually at any time.

LOG="/Users/eoin/.local/bin/weekly-review.log"
REVIEWS_DIR="/Users/eoin/knowledge_base/_reviews"
QUERY_GRAPH="/Users/eoin/query_graph.py"

mkdir -p "$REVIEWS_DIR"

WEEK=$(date +%G-W%V)
TODAY=$(date +%Y-%m-%d)
OUT="$REVIEWS_DIR/${WEEK}.md"

echo "$(date): Generating weekly review for $WEEK..." >> "$LOG"

{
    echo "---"
    echo "title: \"Weekly Review $WEEK\""
    echo "date: $TODAY"
    echo "type: weekly_review"
    echo "---"
    echo
    # --weeks 2 because this fires Monday morning: weeks_back=2 covers
    # the week that just ended (Mon..Sun) plus today, which is the useful
    # frame for a Monday review. With --weeks 1 the range is just today.
    /usr/local/bin/python3 "$QUERY_GRAPH" review --weeks 2
} > "$OUT" 2>>"$LOG"

if [ ${PIPESTATUS[0]:-0} -eq 0 ] && [ -s "$OUT" ]; then
    echo "$(date): Wrote $OUT ($(wc -l < "$OUT") lines)" >> "$LOG"
else
    echo "$(date): FAILED — see output at $OUT" >> "$LOG"
    exit 1
fi
