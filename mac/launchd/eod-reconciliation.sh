#!/bin/bash
# End-of-day reconciliation. Runs at 19:00 (after the user's commit-by-EOD
# window). Refreshes calendars one final time, rebuilds, and reports any
# meetings whose calendar match changed during the day. Re-runs speaker ID
# on any unconfirmed meetings whose attendees shifted.

LOG="/Users/eoin/.local/bin/eod-reconciliation.log"
REPORT_DIR="$HOME/.local/share/kb/reconciliation"
mkdir -p "$REPORT_DIR"

TODAY=$(date +%Y-%m-%d)
SNAP_BEFORE="$REPORT_DIR/${TODAY}_before.json"
SNAP_AFTER="$REPORT_DIR/${TODAY}_after.json"
REPORT="$REPORT_DIR/${TODAY}_report.md"

echo "$(date): EOD reconciliation starting for $TODAY" >> "$LOG"

# Step 1 — Snapshot today's KB meetings BEFORE rebuild
/usr/local/bin/python3 \
    /Users/eoin/knowledgebase-pipeline/mac/eod_reconciliation.py \
    snapshot --date "$TODAY" --out "$SNAP_BEFORE" >> "$LOG" 2>&1

# Step 2 — Refresh calendar export (settled state by EOD)
echo "$(date): Refreshing calendar exports..." >> "$LOG"
/bin/bash /Users/eoin/.local/bin/export-calendars.sh >> "$LOG" 2>&1 || \
    echo "$(date): Calendar export failed (non-fatal)" >> "$LOG"

# Step 3 — Rebuild KB so new calendar state propagates
echo "$(date): Rebuilding knowledge base..." >> "$LOG"
/usr/local/bin/python3 /Users/eoin/build_knowledge_base.py >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): build_knowledge_base.py FAILED — aborting reconciliation" >> "$LOG"
    exit 1
fi

# Step 4 — Snapshot AFTER and diff
/usr/local/bin/python3 \
    /Users/eoin/knowledgebase-pipeline/mac/eod_reconciliation.py \
    snapshot --date "$TODAY" --out "$SNAP_AFTER" >> "$LOG" 2>&1

/usr/local/bin/python3 \
    /Users/eoin/knowledgebase-pipeline/mac/eod_reconciliation.py \
    diff "$SNAP_BEFORE" "$SNAP_AFTER" --reid > "$REPORT" 2>> "$LOG"

CHANGED=$(grep -c '^### ' "$REPORT" 2>/dev/null || echo 0)
echo "$(date): Reconciliation complete — $CHANGED meeting(s) changed today; report at $REPORT" >> "$LOG"

# Step 5 — Rsync updated KB to Ubuntu so downstream tools see the new state
rsync -az --delete \
    -e "ssh -o StrictHostKeyChecking=no" \
    /Users/eoin/knowledge_base/ "eoin@nvidiaubuntubox:/home/eoin/knowledge_base/" >> "$LOG" 2>&1
echo "$(date): EOD reconciliation done." >> "$LOG"
