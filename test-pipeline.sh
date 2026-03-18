#!/bin/bash
# Daily pipeline health check.
# Runs at 6am (after the 4am KB rebuild completes).
# Sends a macOS notification with pass/fail summary and logs details.

LOG="/Users/eoin/.local/bin/test-pipeline.log"
UBUNTU="eoin@nvidiaubuntubox"
CSV_PATH="/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes Analysis/classification.csv"
KB_DIR="/Users/eoin/knowledge_base"
SYNC_LOG="/Users/eoin/.local/bin/sync-knowledge-base.log"
REBUILD_LOG="/Users/eoin/.local/bin/rebuild-knowledge-base.log"

echo "" >> "$LOG"
echo "$(date): ── Daily pipeline test ──────────────────────────────" >> "$LOG"

PASS=0
FAIL=0
WARN=0
REPORT=""

check_pass() { PASS=$((PASS+1)); REPORT="$REPORT\n✓ $1"; echo "  PASS: $1" >> "$LOG"; }
check_fail() { FAIL=$((FAIL+1)); REPORT="$REPORT\n✗ $1"; echo "  FAIL: $1" >> "$LOG"; }
check_warn() { WARN=$((WARN+1)); REPORT="$REPORT\n⚠ $1"; echo "  WARN: $1" >> "$LOG"; }

# ── 1. Ubuntu: notes-watcher running ─────────────────────────────────────────
echo "Checking Ubuntu services..." >> "$LOG"
if ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$UBUNTU" \
    "systemctl is-active notes-watcher" > /dev/null 2>&1; then
    check_pass "notes-watcher running on Ubuntu"
else
    check_fail "notes-watcher NOT running on Ubuntu"
fi

# ── 2. Ubuntu: litellm running ────────────────────────────────────────────────
if ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$UBUNTU" \
    "systemctl --user is-active litellm" > /dev/null 2>&1; then
    check_pass "LiteLLM proxy running on Ubuntu"
else
    check_warn "LiteLLM proxy not running on Ubuntu"
fi

# ── 3. CSV: updated in last 48h ───────────────────────────────────────────────
echo "Checking CSV..." >> "$LOG"
if [ -f "$CSV_PATH" ]; then
    csv_age_hours=$(( ( $(date +%s) - $(date -r "$CSV_PATH" +%s) ) / 3600 ))
    csv_rows=$(wc -l < "$CSV_PATH")
    echo "  CSV: $csv_rows rows, last modified ${csv_age_hours}h ago" >> "$LOG"
    if [ "$csv_age_hours" -le 48 ]; then
        check_pass "CSV updated ${csv_age_hours}h ago ($csv_rows rows)"
    else
        check_warn "CSV not updated in ${csv_age_hours}h — new recordings may not be processing"
    fi
else
    check_fail "CSV not found at $CSV_PATH"
fi

# ── 4. KB: rebuilt successfully ───────────────────────────────────────────────
echo "Checking KB rebuild log..." >> "$LOG"
if [ -f "$REBUILD_LOG" ]; then
    last_rebuild=$(grep "KB rebuild complete" "$REBUILD_LOG" | tail -1)
    if [ -n "$last_rebuild" ]; then
        rebuild_ts=$(echo "$last_rebuild" | awk '{print $1, $2}')
        check_pass "Daily rebuild completed (last: $rebuild_ts)"
    else
        last_fail=$(grep "FAILED" "$REBUILD_LOG" | tail -1)
        if [ -n "$last_fail" ]; then
            check_fail "Daily rebuild last failed: $(echo "$last_fail" | cut -c1-60)"
        else
            check_warn "No completed rebuild found in log"
        fi
    fi
else
    check_warn "Rebuild log not found yet"
fi

# ── 5. KB: incremental sync activity ─────────────────────────────────────────
echo "Checking incremental sync log..." >> "$LOG"
if [ -f "$SYNC_LOG" ]; then
    last_sync=$(grep "Incremental sync done" "$SYNC_LOG" | tail -1)
    sync_errors=$(grep -c "FAILED\|Error" "$SYNC_LOG" 2>/dev/null || echo 0)
    if [ -n "$last_sync" ]; then
        sync_ts=$(echo "$last_sync" | awk '{print $1, $2}')
        check_pass "Last incremental sync: $sync_ts"
    else
        check_warn "No successful incremental syncs yet"
    fi
    if [ "$sync_errors" -gt 0 ]; then
        check_warn "$sync_errors error(s) in sync log"
    fi
else
    check_warn "Sync log not found yet"
fi

# ── 6. KB: file counts ────────────────────────────────────────────────────────
echo "Checking KB file counts..." >> "$LOG"
if [ -d "$KB_DIR" ]; then
    meetings=$(ls "$KB_DIR/meetings/"*.md 2>/dev/null | wc -l | tr -d ' ')
    people=$(ls "$KB_DIR/people/"*.md 2>/dev/null | wc -l | tr -d ' ')
    echo "  KB files: $meetings meetings, $people people" >> "$LOG"
    if [ "$meetings" -gt 0 ]; then
        check_pass "KB has $meetings meeting files, $people people files"
    else
        check_fail "KB meetings directory is empty"
    fi
else
    check_fail "KB directory not found"
fi

# ── 7. Open WebUI reachable ───────────────────────────────────────────────────
echo "Checking Open WebUI..." >> "$LOG"
webui_status=$(curl -s -o /dev/null -w "%{http_code}" \
    --connect-timeout 10 "http://100.121.184.27:8080/health" 2>/dev/null)
if [ "$webui_status" = "200" ]; then
    # Check collection file count
    token=$(curl -s -X POST "http://100.121.184.27:8080/api/v1/auths/signin" \
        -H "Content-Type: application/json" \
        -d '{"email":"eoinlane@gmail.com","password":"el"}' \
        --connect-timeout 10 2>/dev/null | \
        python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))" 2>/dev/null)
    if [ -n "$token" ]; then
        file_count=$(ssh -o ConnectTimeout=10 "$UBUNTU" \
            "docker exec open-webui python3 -c \"
import sqlite3
db = sqlite3.connect('/app/backend/data/webui.db')
rows = db.execute('SELECT COUNT(*) FROM knowledge_file').fetchone()
print(rows[0])
\"" 2>/dev/null)
        check_pass "Open WebUI reachable — $file_count total files in KB"
    else
        check_warn "Open WebUI reachable but auth failed"
    fi
else
    check_fail "Open WebUI not reachable (status: $webui_status)"
fi

# ── 8. Ubuntu: transcription pipeline activity ───────────────────────────────
echo "Checking transcription activity..." >> "$LOG"
recent_transcripts=$(ssh -o ConnectTimeout=10 "$UBUNTU" \
    "find ~/audio-inbox/Transcriptions -name '*.txt' -mtime -2 2>/dev/null | wc -l" 2>/dev/null)
if [ -n "$recent_transcripts" ] && [ "$recent_transcripts" -gt 0 ]; then
    check_pass "$recent_transcripts transcript(s) processed in last 48h"
else
    check_warn "No new transcripts in last 48h (no new recordings, or pipeline issue)"
fi

# ── Summary notification ──────────────────────────────────────────────────────
echo "" >> "$LOG"
echo "Result: $PASS passed, $WARN warnings, $FAIL failed" >> "$LOG"

if [ "$FAIL" -gt 0 ]; then
    STATUS="❌ Pipeline: $FAIL failure(s)"
    SOUND="Basso"
elif [ "$WARN" -gt 0 ]; then
    STATUS="⚠️  Pipeline: $WARN warning(s)"
    SOUND="Funk"
else
    STATUS="✅ Pipeline: all $PASS checks passed"
    SOUND="Glass"
fi

BODY=$(printf "$REPORT" | sed 's/\\n/\n/g')

osascript << ASEOF
display notification "$BODY" with title "$STATUS" sound name "$SOUND"
ASEOF

echo "$(date): Notification sent — $STATUS" >> "$LOG"
