#!/bin/bash
# pipeline-health-check.sh
# Runs hourly via launchd. Sends a macOS notification if the pipeline looks stuck.

UBUNTU="eoin@nvidiaubuntubox"
LOG="/Users/eoin/.local/bin/pipeline-health-check.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" >> "$LOG"; }
alert() {
    local title="$1" msg="$2"
    osascript -e "display notification \"$msg\" with title \"Pipeline Alert\" subtitle \"$title\" sound name \"Basso\"" 2>/dev/null
    log "ALERT — $title: $msg"
}

log "--- Health check starting ---"

# ── 1. Ubuntu reachable? ──────────────────────────────────────────────────────
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$UBUNTU" "true" 2>/dev/null; then
    alert "Ubuntu unreachable" "Cannot SSH to nvidiaubuntubox"
    log "--- Health check done ---"
    exit 1
fi

# ── 1b. FreeBSD host reachable? (via Tailscale hostname, works on/off LAN) ───
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes eoin@freebsd "true" 2>/dev/null; then
    alert "FreeBSD host unreachable" "Cannot SSH to freebsd — ollama-box VM cannot run"
fi

# ── 1c. ollama-box responsive? (check via Ubuntu since Mac may be off-LAN) ──
OLLAMA_RESP=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$UBUNTU" \
    "curl -s --max-time 10 http://192.168.0.70:11434/api/tags" 2>/dev/null)
if [ -z "$OLLAMA_RESP" ]; then
    alert "ollama-box not responding" "192.168.0.70:11434 unreachable from Ubuntu — classifications will fail"
fi

# ── 1d. ollama-box GPU in use? ───────────────────────────────────────────────
OLLAMA_VRAM=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$UBUNTU" \
    "ssh -o ConnectTimeout=5 -o BatchMode=yes eoin@192.168.0.70 'nvidia-smi --query-compute-apps=used_memory --format=csv,noheader,nounits 2>/dev/null | awk \"{s+=\\$1} END{print (s+0)}\"'" 2>/dev/null)
if [ -n "$OLLAMA_RESP" ] && [ "${OLLAMA_VRAM:-0}" -lt 100 ]; then
    alert "ollama-box GPU idle" "Ollama responding but 0MB VRAM — model may have unloaded"
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

log "--- Health check done (pending=${PENDING}, newest=${NEWEST_AGE}h, skips=${RECENT_SKIPS}/${RECENT_RUNS}) ---"
