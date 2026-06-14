#!/bin/bash
# Weekly synthesis. Calls query_graph.py synthesise --project X for each
# active client project, concatenates the per-project narratives into one
# markdown doc, and emails it. Designed for Sunday 18:00 IST so it lands
# in the inbox before Monday's weekly review at 07:00.
#
# Why a separate scheduled push: the synthesise CLI was used 4 times in
# the 2 weeks after it shipped, then forgotten. Putting it on a schedule
# turns a never-remembered pull into a guaranteed weekly read.
#
# Cost: ~7 × ~$0.15 Opus 4.7 calls = ~$1/week. Cheap relative to the
# strategic value of seeing trajectory across all live engagements.

LOG="/Users/eoin/.local/bin/weekly-synthesis.log"
SYNTH_DIR="/Users/eoin/knowledge_base/_syntheses"
QUERY_GRAPH="/Users/eoin/query_graph.py"
STABLE="/Users/eoin/weekly_synthesis.md"
EMAILER="/Users/eoin/morning_brief_emailer.py"

# Active client projects + new-business pipeline (FutureBusiness — added
# 2026-06-14 as a priority lane, was previously excluded as low-volume
# noise). Excludes other:* still. DFB is part of the DCC umbrella so it
# lives inside DCC's synthesis rather than getting its own.
PROJECTS=("FutureBusiness" "NTA" "DCC" "Diotima" "Paradigm" "ADAPT" "TBS" "LCC")

mkdir -p "$SYNTH_DIR"
TODAY=$(date +%Y-%m-%d)
OUT="$SYNTH_DIR/${TODAY}.md"

echo "$(date): === Weekly synthesis START ===" >> "$LOG"

{
    echo "---"
    echo "title: \"Weekly Synthesis $TODAY\""
    echo "date: $TODAY"
    echo "type: weekly_synthesis"
    echo "---"
    echo
    echo "# Weekly Synthesis — $(date '+%A %d %B %Y')"
    echo
    echo "_Strategic narrative across active client projects. Opus 4.7. Generated weekly Sundays 18:00 IST._"
    echo

    for proj in "${PROJECTS[@]}"; do
        echo "$(date): synthesising $proj..." >> "$LOG"
        # synthesise stdout starts with a "Synthesising ..." status line we
        # don't want in the email, then prints "# Synthesis: X" + body. Drop
        # the status line, keep the rest.
        BODY=$(/usr/local/bin/python3 "$QUERY_GRAPH" synthesise --project "$proj" 2>>"$LOG" \
               | sed -n '/^# Synthesis:/,$p')
        if [ -n "$BODY" ]; then
            # Demote the H1 to H2 so the email's top-level title stays the top header
            echo "$BODY" | sed 's/^# Synthesis: /## /'
            echo
            echo "---"
            echo
        else
            echo "## $proj"
            echo
            echo "_Synthesis skipped — no meetings on file or LLM call failed (see log)._"
            echo
            echo "---"
            echo
        fi
    done
} > "$OUT" 2>>"$LOG"

cp "$OUT" "$STABLE"
echo "$(date): wrote $(wc -l < "$OUT") lines → $OUT" >> "$LOG"

# Email — reuses the morning brief sender; subject "Weekly Synthesis" so it
# threads with itself rather than the daily brief.
if [ -x "$EMAILER" ]; then
    if /usr/local/bin/python3 "$EMAILER" --file "$STABLE" --subject "Weekly Synthesis" >> "$LOG" 2>&1; then
        echo "$(date): email sent" >> "$LOG"
    else
        echo "$(date): email FAILED (markdown still at $STABLE)" >> "$LOG"
    fi
fi

echo "$(date): === Weekly synthesis END ===" >> "$LOG"
exit 0
