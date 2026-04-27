#!/bin/bash
# Sync Plaud-style audio (.mp3, .m4a) dropped into ~/inbox/ to Ubuntu's
# ~/audio-inbox/Notes/ where the transcription pipeline picks them up.
# Triggered by launchd WatchPaths on ~/inbox/. After a successful upload
# (or a no-op when the same name already exists on Ubuntu), the local
# file is moved to ~/inbox/done/ to avoid re-processing.
#
# Idempotent: uses rsync --ignore-existing so a file that's already on
# Ubuntu is skipped, then the local copy is filed away. Files with
# " copy " or "_copy" in the name (Finder duplicates) are skipped.

LOG="/Users/eoin/.local/bin/sync-inbox-audio.log"
LOCK="/tmp/sync-inbox-audio.lock"
INBOX="/Users/eoin/inbox"
DONE="$INBOX/done"
UBUNTU_TARGET="nvidiaubuntubox:audio-inbox/Notes/"

if [ -f "$LOCK" ]; then
    lock_pid=$(cat "$LOCK" 2>/dev/null)
    if kill -0 "$lock_pid" 2>/dev/null; then
        echo "$(date): Already running (PID $lock_pid), skipping" >> "$LOG"
        exit 0
    fi
fi
echo $$ > "$LOCK"
trap "rm -f '$LOCK'" EXIT

mkdir -p "$DONE"

shopt -s nullglob
found=0
for f in "$INBOX"/*.mp3 "$INBOX"/*.m4a; do
    [ -f "$f" ] || continue
    name=$(basename "$f")
    case "$name" in
        *" copy"*|*"_copy"*)
            echo "$(date): Skipping Finder duplicate: $name" >> "$LOG"
            continue
            ;;
    esac
    found=1
    echo "$(date): Uploading $name to Ubuntu..." >> "$LOG"
    if rsync -a --ignore-existing --partial \
            -e "ssh -o StrictHostKeyChecking=no" \
            "$f" "$UBUNTU_TARGET" >> "$LOG" 2>&1; then
        if mv -n "$f" "$DONE/" 2>>"$LOG"; then
            echo "$(date): Done $name (filed in ~/inbox/done/)" >> "$LOG"
        else
            echo "$(date): Uploaded $name but local move FAILED" >> "$LOG"
        fi
    else
        echo "$(date): Upload FAILED for $name (will retry on next trigger)" >> "$LOG"
    fi
done

[ "$found" -eq 0 ] && echo "$(date): No audio files in inbox" >> "$LOG"
