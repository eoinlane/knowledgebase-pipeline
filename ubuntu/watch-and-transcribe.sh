#!/bin/bash
# Watches ~/audio-inbox/Notes/ for new .m4a files.
# Transcribes with WhisperX+diarization, classifies with Ollama, syncs results to Mac.

AUDIO_DIR="/home/eoin/audio-inbox/Notes"
OUT_DIR="/home/eoin/audio-inbox/Transcriptions"
LOG="/home/eoin/audio-inbox/transcribe.log"
VENV="/home/eoin/whisper-env"
MAC_HOST="eoin@100.103.128.44"
MAC_NOTES_DIR="/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes"
MAC_ANALYSIS_DIR="/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes Analysis"
CSV_PATH="/home/eoin/audio-inbox/classification.csv"
# HF_TOKEN is set in the systemd service environment (notes-watcher)
# Do not hardcode secrets in scripts

ollama_unload() {
    : # No-op: model stays warm on dedicated ollama-box GPU
}

mkdir -p "$OUT_DIR"
echo "$(date): Watcher started" >> "$LOG"

# Sync CSV from Mac on startup to ensure we have latest
rsync -az -e "ssh -o StrictHostKeyChecking=no" \
    "$MAC_HOST:$MAC_ANALYSIS_DIR/classification.csv" "$CSV_PATH" >> "$LOG" 2>&1 || true

inotifywait -m -e close_write -e moved_to "$AUDIO_DIR" --format "%f" |
while read FNAME; do
    [[ "$FNAME" != *.m4a ]] && continue

    UUID="${FNAME%.m4a}"
    OUT_PATH="$OUT_DIR/$UUID.txt"

    if [ -f "$OUT_PATH" ]; then
        echo "$(date): Already transcribed — $FNAME" >> "$LOG"
        continue
    fi

    # --- TRANSCRIBE ---
    echo "$(date): Transcribing $FNAME..." >> "$LOG"
    source "$VENV/bin/activate"
    python3 /home/eoin/transcribe_single.py "$AUDIO_DIR/$FNAME" "$OUT_PATH" >> "$LOG" 2>&1
    TRANS_STATUS=$?
    deactivate

    if [ $TRANS_STATUS -ne 0 ]; then
        echo "$(date): Transcription FAILED (exit $TRANS_STATUS) — $FNAME" >> "$LOG"
        continue
    fi
    echo "$(date): Transcribed OK — $UUID.txt" >> "$LOG"

    # Sync transcript to Mac
    rsync -az -e "ssh -o StrictHostKeyChecking=no" \
        "$OUT_PATH" "$MAC_HOST:$MAC_NOTES_DIR/" >> "$LOG" 2>&1
    if [ $? -eq 0 ]; then
        echo "$(date): Transcript synced to Mac — $UUID.txt" >> "$LOG"
    else
        echo "$(date): Transcript sync FAILED — $UUID.txt" >> "$LOG"
    fi

    # --- CLASSIFY ---
    echo "$(date): Classifying $UUID..." >> "$LOG"
    source "$VENV/bin/activate"
    python3 /home/eoin/classify_transcript.py "$OUT_PATH" "$CSV_PATH" >> "$LOG" 2>&1
    CLASS_STATUS=$?
    deactivate
    ollama_unload  # always release GPU after Ollama, even on timeout/failure

    if [ $CLASS_STATUS -ne 0 ]; then
        echo "$(date): Classification FAILED (exit $CLASS_STATUS) — $UUID" >> "$LOG"
        continue
    fi
    echo "$(date): Classified OK — $UUID" >> "$LOG"

    # Sync updated CSV to Mac
    rsync -az -e "ssh -o StrictHostKeyChecking=no" \
        "$CSV_PATH" "$MAC_HOST:$MAC_ANALYSIS_DIR/classification.csv" >> "$LOG" 2>&1
    if [ $? -eq 0 ]; then
        echo "$(date): CSV synced to Mac" >> "$LOG"
    else
        echo "$(date): CSV sync FAILED" >> "$LOG"
    fi

    # --- IDENTIFY SPEAKERS ---
    echo "$(date): Identifying speakers for $UUID..." >> "$LOG"
    source "$VENV/bin/activate"
    python3 /home/eoin/identify_speakers.py "$OUT_PATH" "$CSV_PATH" >> "$LOG" 2>&1
    ID_STATUS=$?
    deactivate
    ollama_unload  # always release GPU after Ollama, even on timeout/failure

    if [ $ID_STATUS -ne 0 ]; then
        echo "$(date): Speaker ID FAILED (exit $ID_STATUS) — $UUID" >> "$LOG"
    else
        echo "$(date): Speaker ID OK — $UUID" >> "$LOG"
        # Re-sync transcript with speaker names written in
        rsync -az -e "ssh -o StrictHostKeyChecking=no" \
            "$OUT_PATH" "$MAC_HOST:$MAC_NOTES_DIR/" >> "$LOG" 2>&1
        if [ $? -eq 0 ]; then
            echo "$(date): Updated transcript synced to Mac — $UUID.txt" >> "$LOG"
        else
            echo "$(date): Updated transcript sync FAILED — $UUID.txt" >> "$LOG"
        fi
    fi
done
