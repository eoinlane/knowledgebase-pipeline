#!/bin/bash
# Fast incremental KB sync — triggered by CSV changes via launchd WatchPaths.
# Rebuilds KB markdown and rsyncs to Ubuntu. Uses a lock file to prevent overlap.
# (Open WebUI upload step retired 2026-04-27; KB queries via Claude Code + query_graph.py.)

LOG="/Users/eoin/.local/bin/sync-knowledge-base.log"
LOCK="/tmp/sync-knowledge-base.lock"
UBUNTU="eoin@nvidiaubuntubox"

# ── Lock ──────────────────────────────────────────────────────────────────────
if [ -f "$LOCK" ]; then
    lock_pid=$(cat "$LOCK" 2>/dev/null)
    if kill -0 "$lock_pid" 2>/dev/null; then
        echo "$(date): Already running (PID $lock_pid), skipping" >> "$LOG"
        exit 0
    fi
fi
echo $$ > "$LOCK"
trap "rm -f '$LOCK'" EXIT

echo "$(date): Incremental sync starting..." >> "$LOG"

# Wait for iCloud to finish syncing the CSV — iCloud can hold file locks for
# several minutes during active syncs, so give it a generous head start.
sleep 60

# ── Step 0: Refresh calendar exports ──────────────────────────────────────────
# Run before every build so meetings moved/added/cancelled during the day are
# reflected. The 4am rebuild's snapshot would otherwise be stale by the time
# midday recordings get processed. Non-fatal — if calendar export fails the
# build can still use the most recent good cache (~/.local/share/kb/calendars/).
echo "$(date): Refreshing calendar exports..." >> "$LOG"
/bin/bash /Users/eoin/.local/bin/export-calendars.sh >> "$LOG" 2>&1 || \
    echo "$(date): Calendar export failed — using cached files" >> "$LOG"

# ── Step 1: Build KB markdown ─────────────────────────────────────────────────
echo "$(date): Building KB..." >> "$LOG"
/usr/local/bin/python3 /Users/eoin/build_knowledge_base.py >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): Build FAILED" >> "$LOG"
    exit 1
fi

# ── Step 2: Rsync to Ubuntu ───────────────────────────────────────────────────
echo "$(date): Rsyncing to Ubuntu..." >> "$LOG"
rsync -az --delete \
    -e "ssh -o StrictHostKeyChecking=no" \
    /Users/eoin/knowledge_base/ "$UBUNTU:/home/eoin/knowledge_base/" >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): rsync FAILED" >> "$LOG"
    exit 1
fi

echo "$(date): Incremental sync done" >> "$LOG"
