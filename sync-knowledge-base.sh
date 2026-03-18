#!/bin/bash
# Fast incremental KB sync — triggered by CSV changes via launchd WatchPaths.
# Rebuilds KB markdown, rsyncs to Ubuntu, then incrementally uploads only
# new/changed files to Open WebUI. Uses a lock file to prevent overlap.

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

# ── Step 1: Build KB markdown ─────────────────────────────────────────────────
# Cal files in /tmp are refreshed by the daily 4am job.
# If they're missing (e.g. after reboot), build still works without calendar data.
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

# ── Step 3: Incremental upload to Open WebUI ─────────────────────────────────
echo "$(date): Incremental upload..." >> "$LOG"
/usr/local/bin/python3 /Users/eoin/upload_knowledge_base_incremental.py >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): Upload FAILED" >> "$LOG"
    exit 1
fi

echo "$(date): Incremental sync done" >> "$LOG"
