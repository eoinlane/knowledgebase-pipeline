#!/bin/bash
# watchdog-transcribe.sh
# Runs every 30 min via systemd timer. Catches audio files the watcher missed
# (CUDA OOM, non-.m4a formats, inotify race conditions) and retries failed
# classifications. Never runs while transcription is already in progress.

AUDIO_DIR="/home/eoin/audio-inbox/Notes"
TRANS_DIR="/home/eoin/audio-inbox/Transcriptions"
CSV_PATH="/home/eoin/audio-inbox/classification.csv"
LOG="/home/eoin/audio-inbox/watchdog.log"
VENV="/home/eoin/whisper-env"
MAC_HOST="eoin@100.103.128.44"
MAC_NOTES_DIR="/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes"
MAC_ANALYSIS_DIR="/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes Analysis"
MIN_AGE_MINUTES=15   # Give the watcher first shot at new files
OLLAMA_UNLOAD='{"model":"deepseek-r1:32b","keep_alive":0}'

log() { echo "$(date '+%Y-%m-%d %H:%M:%S'): $*" >> "$LOG"; }

ollama_unload() {
    curl -s http://localhost:11434/api/generate -d "$OLLAMA_UNLOAD" > /dev/null 2>&1 || true
}

log "--- Watchdog starting ---"

# Bail out if GPU is in heavy use (avoids CUDA OOM competition).
# Note: pgrep -f is not used — it matches its own command line when the search string appears in it.
GPU_MB=$(nvidia-smi --query-compute-apps=used_memory --format=csv,noheader,nounits 2>/dev/null | awk '{s+=$1} END{print (s+0)}')
if [ "$GPU_MB" -gt 2000 ]; then
    log "GPU busy (${GPU_MB}MB in use) — skipping this run."
    log "--- Watchdog done ---"
    exit 0
fi

# ── Step 1: Retry failed classifications (no GPU needed, just Ollama) ──────────
# Find transcripts with no entry in the CSV — classification previously failed.
RETRY_COUNT=0
while IFS= read -r txt_path; do
    [ "$RETRY_COUNT" -ge 5 ] && break   # max 5 retries per watchdog run
    stem=$(basename "$txt_path" .txt)
    if ! grep -q "$stem" "$CSV_PATH" 2>/dev/null; then
        log "Retrying classification: $stem"
        source "$VENV/bin/activate"
        python3 /home/eoin/classify_transcript.py "$txt_path" "$CSV_PATH" >> "$LOG" 2>&1
        STATUS=$?
        deactivate
        ollama_unload
        if [ $STATUS -eq 0 ]; then
            log "Classification OK — $stem"
            rsync -az -e "ssh -o StrictHostKeyChecking=no" \
                "$CSV_PATH" "$MAC_HOST:$MAC_ANALYSIS_DIR/classification.csv" >> "$LOG" 2>&1
        else
            log "Classification FAILED again — $stem"
        fi
        RETRY_COUNT=$((RETRY_COUNT + 1))
    fi
done < <(find "$TRANS_DIR" -name "*.txt" -mmin +30 | sort)

# ── Step 2: Find oldest untranscribed audio file (.m4a or .mp3) ───────────────
PENDING=""
PENDING_AGE=0

for audio in "$AUDIO_DIR"/*.m4a "$AUDIO_DIR"/*.mp3; do
    [ -f "$audio" ] || continue
    fname=$(basename "$audio")
    stem="${fname%.*}"
    txt="$TRANS_DIR/${stem}.txt"
    [ -f "$txt" ] && continue
    age_minutes=$(( ( $(date +%s) - $(stat -c %Y "$audio") ) / 60 ))
    [ "$age_minutes" -lt "$MIN_AGE_MINUTES" ] && continue
    if [ -z "$PENDING" ] || [ "$age_minutes" -gt "$PENDING_AGE" ]; then
        PENDING="$audio"
        PENDING_AGE="$age_minutes"
    fi
done

if [ -z "$PENDING" ]; then
    log "No pending audio files."
    log "--- Watchdog done ---"
    exit 0
fi

fname=$(basename "$PENDING")
stem="${fname%.*}"
txt="$TRANS_DIR/${stem}.txt"

log "Pending audio ($PENDING_AGE min old): $fname — transcribing..."

# ── Step 3: Transcribe ────────────────────────────────────────────────────────
source "$VENV/bin/activate"
python3 /home/eoin/transcribe_single.py "$PENDING" "$txt" >> "$LOG" 2>&1
STATUS=$?
deactivate

if [ $STATUS -ne 0 ]; then
    log "Transcription FAILED — $fname"
    log "--- Watchdog done ---"
    exit 1
fi
log "Transcribed OK — ${stem}.txt"

rsync -az -e "ssh -o StrictHostKeyChecking=no" \
    "$txt" "$MAC_HOST:$MAC_NOTES_DIR/" >> "$LOG" 2>&1

# ── Step 4: Classify ──────────────────────────────────────────────────────────
log "Classifying $stem..."
source "$VENV/bin/activate"
python3 /home/eoin/classify_transcript.py "$txt" "$CSV_PATH" >> "$LOG" 2>&1
STATUS=$?
deactivate
ollama_unload

if [ $STATUS -eq 0 ]; then
    log "Classified OK — $stem"
    rsync -az -e "ssh -o StrictHostKeyChecking=no" \
        "$CSV_PATH" "$MAC_HOST:$MAC_ANALYSIS_DIR/classification.csv" >> "$LOG" 2>&1
else
    log "Classification FAILED — $stem (will retry next run)"
fi

# ── Step 5: Speaker ID ────────────────────────────────────────────────────────
log "Speaker ID for $stem..."
source "$VENV/bin/activate"
python3 /home/eoin/identify_speakers.py "$txt" "$CSV_PATH" >> "$LOG" 2>&1
STATUS=$?
deactivate
ollama_unload

if [ $STATUS -eq 0 ]; then
    log "Speaker ID OK — $stem"
    rsync -az -e "ssh -o StrictHostKeyChecking=no" \
        "$txt" "$MAC_HOST:$MAC_NOTES_DIR/" >> "$LOG" 2>&1
else
    log "Speaker ID FAILED — $stem"
fi

log "--- Watchdog done ---"
