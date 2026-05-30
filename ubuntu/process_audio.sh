#!/bin/bash
# process_audio.sh — run the full pipeline (transcribe → classify → speaker_id →
# reclassify → insights) on one audio file. Use for backfilling or for one-off
# files that the watchdog hasn't picked up yet.
#
# Usage:
#   ./process_audio.sh /path/to/audio.mp3 [LOG_PATH]
#
# Why this exists: the ad-hoc one-liner I kept writing as
#   `cmd1 && cmd2 && cmd3; echo DONE`
# logs DONE unconditionally even when cmd1/2/3 fail (the && short-circuits but
# the trailing echo runs regardless). That bit me on 2026-05-27 when a CUDA OOM
# during the Alex catch-up transcription wrote DONE to the log and the watcher
# reported success — the CSV was never updated. This script uses
# `set -euo pipefail` + a trap on EXIT to write either DONE or FAILED honestly
# based on rc.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 /path/to/audio.{mp3,m4a} [LOG_PATH]" >&2
    exit 2
fi

AUDIO="$1"
LOG="${2:-/home/eoin/audio-inbox/process_audio.log}"
CSV=/home/eoin/audio-inbox/classification.csv
VENV=/home/eoin/whisper-env

if [ ! -f "$AUDIO" ]; then
    echo "$(date): MISSING $AUDIO" >> "$LOG"
    echo "Audio file not found: $AUDIO" >&2
    exit 2
fi

STEM=$(basename "$AUDIO")
STEM="${STEM%.*}"
TXT=/home/eoin/audio-inbox/Transcriptions/${STEM}.txt
INSIGHTS=/home/eoin/audio-inbox/Insights/${STEM}.json

mkdir -p "$(dirname "$TXT")" "$(dirname "$INSIGHTS")"

# Honest DONE/FAILED marker: trap fires on every exit path (success, error,
# signal). Inside the trap, the saved $rc reflects the actual exit status of
# whichever command was last running when we exited.
finalize() {
    local rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "$(date): === DONE $STEM ===" >> "$LOG"
    else
        echo "$(date): === FAILED $STEM (rc=$rc) ===" >> "$LOG"
    fi
    exit "$rc"
}
trap finalize EXIT

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "$(date): === START $STEM ===" >> "$LOG"

# Stages run in sequence — set -e ensures any non-zero rc kills the script
# and triggers the finalize trap with the real rc.
python3 /home/eoin/transcribe_single.py     "$AUDIO" "$TXT"      >> "$LOG" 2>&1
python3 /home/eoin/classify_transcript.py   "$TXT"   "$CSV"      >> "$LOG" 2>&1
python3 /home/eoin/identify_speakers.py     "$TXT"   "$CSV"      >> "$LOG" 2>&1

# reclassify_by_speaker is best-effort — if it can't override, that's not a
# failure of the pipeline. Wrap in || true so its non-zero rc doesn't abort.
python3 /home/eoin/reclassify_by_speaker.py "$TXT"   "$CSV"      >> "$LOG" 2>&1 || true

python3 /home/eoin/extract_meeting_insights.py "$TXT" "$INSIGHTS" >> "$LOG" 2>&1
