#!/bin/bash
# pipeline-health-check.sh
# Runs hourly via launchd. Sends a macOS notification if the pipeline looks stuck.
#
# Flags:
#   -v / --verbose   Mirror log lines to stdout (useful for manual diagnostics)
#   --summary        Skip alerts, just print one-line summary at end (for ssh/cron)

UBUNTU="eoin@nvidiaubuntubox"
LOG="/Users/eoin/.local/bin/pipeline-health-check.log"

VERBOSE=0
SUMMARY_ONLY=0
for arg in "$@"; do
    case "$arg" in
        -v|--verbose) VERBOSE=1 ;;
        --summary) SUMMARY_ONLY=1; VERBOSE=1 ;;
    esac
done

log() {
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S'): $*"
    echo "$line" >> "$LOG"
    [ "$VERBOSE" -eq 1 ] && echo "$line"
}
alert() {
    local title="$1" msg="$2"
    if [ "$SUMMARY_ONLY" -eq 0 ]; then
        osascript -e "display notification \"$msg\" with title \"Pipeline Alert\" subtitle \"$title\" sound name \"Basso\"" 2>/dev/null
    fi
    log "ALERT — $title: $msg"
}

log "--- Health check starting ---"

# ── 1. Ubuntu reachable? ──────────────────────────────────────────────────────
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$UBUNTU" "true" 2>/dev/null; then
    alert "Ubuntu unreachable" "Cannot SSH to nvidiaubuntubox"
    log "--- Health check done ---"
    exit 1
fi

# ── 1b. ollama-box responsive? (check via Ubuntu since Mac may be off-LAN) ──
# This is the canonical LLM-availability signal: if the API responds and the
# expected model is loaded, classifications will work — the underlying VM,
# GPU, and bhyve host are all implicitly up.
#
# Removed 2026-04-30: previous "1b FreeBSD SSH check" (no `eoin@freebsd` SSH
# alias on this Mac; never resolvable) and "1d GPU VRAM check" (Ubuntu has
# no SSH key trust to ollama-box at 192.168.0.70 — defaulted to 0 MB and
# fired "GPU idle" alerts every hour for months). Both were redundant once
# the API check confirms the model list contains qwen2.5:14b.
OLLAMA_RESP=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$UBUNTU" \
    "curl -s --max-time 10 http://192.168.0.70:11434/api/tags" 2>/dev/null)
if [ -z "$OLLAMA_RESP" ]; then
    alert "ollama-box not responding" "192.168.0.70:11434 unreachable from Ubuntu — classifications will fail"
elif ! echo "$OLLAMA_RESP" | grep -q "qwen2.5:14b"; then
    alert "ollama-box model missing" "API responds but qwen2.5:14b not in model list"
fi

# ── 2. Services running? ──────────────────────────────────────────────────────
WATCHER=$(ssh "$UBUNTU" "systemctl is-active notes-watcher 2>/dev/null")
LITELLM=$(ssh "$UBUNTU" "systemctl --user is-active litellm 2>/dev/null")
TIMER=$(ssh "$UBUNTU" "systemctl is-active transcribe-watchdog.timer 2>/dev/null")

[ "$WATCHER" != "active" ] && alert "notes-watcher down" "Service is $WATCHER — transcription paused"
[ "$LITELLM" != "active" ] && alert "litellm down" "LiteLLM proxy is $LITELLM"
[ "$TIMER" != "active" ] && alert "watchdog timer down" "transcribe-watchdog.timer is $TIMER"

# ── 3. Pending files piling up? ───────────────────────────────────────────────
PENDING=$(ssh "$UBUNTU" "bash -c '
    count=0
    for f in ~/audio-inbox/Notes/*.m4a ~/audio-inbox/Notes/*.mp3; do
        [ -f \"\$f\" ] || continue
        stem=\$(basename \"\$f\"); stem=\"\${stem%.*}\"
        [ ! -f ~/audio-inbox/Transcriptions/\${stem}.txt ] && count=\$((count+1))
    done
    echo \$count
'" 2>/dev/null)

log "Pending files: ${PENDING:-unknown}"
if [ "${PENDING:-0}" -gt 10 ]; then
    alert "Backlog building" "${PENDING} audio files waiting to be transcribed"
fi

# ── 4. Watchdog repeatedly skipping? ─────────────────────────────────────────
# If the last 10 watchdog entries are all "skipping", something is wrong.
SKIP_COUNT=$(ssh "$UBUNTU" "grep -c 'skipping this run' ~/audio-inbox/watchdog.log 2>/dev/null | tail -1" 2>/dev/null)
TOTAL_RUNS=$(ssh "$UBUNTU" "grep -c '--- Watchdog starting ---' ~/audio-inbox/watchdog.log 2>/dev/null" 2>/dev/null)
RECENT_SKIPS=$(ssh "$UBUNTU" "tail -40 ~/audio-inbox/watchdog.log 2>/dev/null | grep -c 'skipping this run'" 2>/dev/null)
RECENT_RUNS=$(ssh "$UBUNTU" "tail -40 ~/audio-inbox/watchdog.log 2>/dev/null | grep -c 'Watchdog starting'" 2>/dev/null)

log "Recent watchdog: ${RECENT_RUNS} runs, ${RECENT_SKIPS} skips"
if [ "${RECENT_RUNS:-0}" -ge 5 ] && [ "${RECENT_SKIPS:-0}" -eq "${RECENT_RUNS:-0}" ]; then
    GPU_MB=$(ssh "$UBUNTU" "nvidia-smi --query-compute-apps=used_memory --format=csv,noheader,nounits 2>/dev/null | awk '{s+=\$1} END{print (s+0)}'" 2>/dev/null)
    if [ "${GPU_MB:-0}" -lt 2000 ]; then
        alert "Watchdog stuck" "Skipping every run but GPU is idle (${GPU_MB}MB). Bug likely."
    fi
fi

# ── 5a. Disk space on Ubuntu ─────────────────────────────────────────────────
DISK_PCT=$(ssh -o ConnectTimeout=10 "$UBUNTU" "df /home/eoin --output=pcent 2>/dev/null | tail -1 | tr -d ' %'" 2>/dev/null)
log "Ubuntu disk: ${DISK_PCT:-unknown}% used"
if [ "${DISK_PCT:-0}" -gt 90 ]; then
    alert "Ubuntu disk nearly full" "${DISK_PCT}% used — writes may fail silently"
fi

# ── 5b. 0-byte insight files (from prior disk-full failures) ────────────────
ZERO_FILES=$(ssh -o ConnectTimeout=10 "$UBUNTU" "find ~/audio-inbox/Insights -name '*.json' -empty 2>/dev/null | wc -l" 2>/dev/null)
log "0-byte insight files: ${ZERO_FILES:-0}"
if [ "${ZERO_FILES:-0}" -gt 0 ]; then
    alert "0-byte insights files" "${ZERO_FILES} empty .json files — insights extraction failed silently"
fi

# ── 5c. Insights lag — transcripts without valid insights ───────────────────
INSIGHTS_LAG=$(ssh -o ConnectTimeout=10 "$UBUNTU" "bash -c '
    count=0
    for t in ~/audio-inbox/Transcriptions/*.txt; do
        [ -f \"\$t\" ] || continue
        uuid=\$(basename \"\$t\" .txt)
        ins=~/audio-inbox/Insights/\${uuid}.json
        if [ ! -f \"\$ins\" ] || [ ! -s \"\$ins\" ]; then
            count=\$((count+1))
        fi
    done
    echo \$count
'" 2>/dev/null)
log "Transcripts without insights: ${INSIGHTS_LAG:-unknown}"
if [ "${INSIGHTS_LAG:-0}" -gt 150 ]; then
    alert "Insights backlog growing" "${INSIGHTS_LAG} transcripts have no insights JSON (was ~134 at baseline)"
fi

# ── 5d. graph.db staleness ──────────────────────────────────────────────────
if [ -f "$HOME/graph.db" ]; then
    GRAPH_AGE=$(( ( $(date +%s) - $(stat -f %m "$HOME/graph.db") ) / 3600 ))
    log "graph.db age: ${GRAPH_AGE}h"
    if [ "$GRAPH_AGE" -gt 48 ]; then
        alert "graph.db stale" "Last rebuilt ${GRAPH_AGE}h ago"
    fi
fi

# ── 5. Last transcription too old? ───────────────────────────────────────────
LAST_TRANS=$(ssh "$UBUNTU" "find ~/audio-inbox/Transcriptions -name '*.txt' -not -empty -newer ~/audio-inbox/Transcriptions/EAE996C7-DA30-4D4A-8E9F-8F5D2E13BAA8.txt | wc -l" 2>/dev/null)
NEWEST_AGE=$(ssh "$UBUNTU" "bash -c '
    newest=\$(find ~/audio-inbox/Transcriptions -name \"*.txt\" -not -empty -printf \"%T@ %p\n\" 2>/dev/null | sort -rn | head -1 | cut -d\" \" -f1)
    [ -n \"\$newest\" ] && echo \$(( (\$(date +%s) - \${newest%.*}) / 3600 )) || echo 999
'" 2>/dev/null)

log "Newest transcript age: ${NEWEST_AGE}h"
if [ "${NEWEST_AGE:-0}" -gt 24 ] && [ "${PENDING:-0}" -gt 0 ]; then
    alert "No transcription in 24h" "Newest transcript is ${NEWEST_AGE}h old with ${PENDING} files pending"
fi

# ── 5e. Pipeline manifest check (stalled/failed recordings) ────────────────
MANIFEST_STATUS=$(ssh -o ConnectTimeout=10 "$UBUNTU" "python3 ~/manifest.py summary 2>/dev/null" 2>/dev/null)
if [ -n "$MANIFEST_STATUS" ]; then
    log "Manifest: $MANIFEST_STATUS"
    MANIFEST_FAILED=$(echo "$MANIFEST_STATUS" | grep -o 'failed=[0-9]*' | cut -d= -f2)
    MANIFEST_STALLED=$(echo "$MANIFEST_STATUS" | grep -o 'stalled=[0-9]*' | cut -d= -f2)
    [ "${MANIFEST_FAILED:-0}" -gt 0 ] && alert "Pipeline failures" "${MANIFEST_FAILED} recordings have failed stages"
    [ "${MANIFEST_STALLED:-0}" -gt 0 ] && alert "Pipeline stalled" "${MANIFEST_STALLED} recordings stuck in processing"
fi

# ── 6. CSV rows vs KB meeting files — catch-up if KB is behind ──────────────
CSV_PATH="/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes Analysis/classification.csv"
if [ -f "$CSV_PATH" ]; then
    CSV_ROWS=$(wc -l < "$CSV_PATH" | tr -d ' ')
    KB_MEETINGS=$(ls /Users/eoin/knowledge_base/meetings/*.md 2>/dev/null | wc -l | tr -d ' ')
    # Count content rows (exclude blank/personal categories that don't generate KB files)
    CSV_CONTENT=$(grep -v 'other:blank' "$CSV_PATH" 2>/dev/null | wc -l | tr -d ' ')

    log "CSV rows: ${CSV_ROWS}, KB meetings: ${KB_MEETINGS}"

    # Check if new CSV entries exist that aren't in the KB yet
    # Get the newest CSV date and newest KB meeting date
    NEWEST_CSV_DATE=$(tail -1 "$CSV_PATH" | cut -d',' -f2 | cut -d' ' -f1)
    NEWEST_KB=$(ls -1 /Users/eoin/knowledge_base/meetings/*.md 2>/dev/null | sort | tail -1 | xargs basename 2>/dev/null | cut -d'_' -f1)

    if [ -n "$NEWEST_CSV_DATE" ] && [ -n "$NEWEST_KB" ] && [ "$NEWEST_CSV_DATE" \> "$NEWEST_KB" ]; then
        # CSV has entries newer than the most recent KB file — KB is behind
        SYNC_LOCK="/tmp/sync-knowledge-base.lock"
        REBUILD_LOCK="/tmp/rebuild-knowledge-base.lock"
        if [ ! -f "$SYNC_LOCK" ] && [ ! -f "$REBUILD_LOCK" ]; then
            alert "KB catch-up" "CSV has entries from ${NEWEST_CSV_DATE} but KB only up to ${NEWEST_KB} — triggering rebuild"
            log "Triggering catch-up rebuild..."
            # Run sync script in background (it handles its own locking)
            nohup /bin/bash /Users/eoin/.local/bin/sync-knowledge-base.sh >> "$LOG" 2>&1 &
        else
            log "KB behind but sync/rebuild already running — skipping catch-up"
        fi
    fi
fi

log "--- Health check done (pending=${PENDING}, newest=${NEWEST_AGE}h, skips=${RECENT_SKIPS}/${RECENT_RUNS}) ---"
