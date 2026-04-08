#!/bin/bash
# Syncs new .m4a files from iCloud "My Notes Audio" to Ubuntu for transcription.
# Copies to /tmp first to avoid iCloud mmap locking (EDEADLK), then rsyncs to Ubuntu.
# Runs via launchd every 5 minutes.

AUDIO_DIR="/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes Audio"
TMP_DIR="/tmp/notes-audio-sync"
UBUNTU_HOST="eoin@nvidiaubuntubox"
UBUNTU_DIR="/home/eoin/audio-inbox/Notes"
LOG="/Users/eoin/.local/bin/sync-notes-audio.log"

echo "$(date): Sync starting..." >> "$LOG"

# Step 1: Copy new .m4a files to /tmp (bypasses iCloud mmap lock)
mkdir -p "$TMP_DIR"
find "$AUDIO_DIR" -name "*.m4a" | while read -r src; do
    fname=$(basename "$src")
    dst="$TMP_DIR/$fname"
    if [ ! -f "$dst" ]; then
        # Use cat to avoid fcopyfile/mmap lock on iCloud files being actively synced
        cat "$src" > "$dst" 2>> "$LOG" && echo "$(date): Copied $fname to /tmp" >> "$LOG" || rm -f "$dst"
    fi
done

# Step 2: Rsync from /tmp to Ubuntu
rsync -az --ignore-existing \
    --include="*.m4a" \
    --exclude="*" \
    -e "ssh -o StrictHostKeyChecking=no" \
    "$TMP_DIR/" "$UBUNTU_HOST:$UBUNTU_DIR/" >> "$LOG" 2>&1

if [ $? -eq 0 ]; then
    echo "$(date): Sync OK" >> "$LOG"
else
    echo "$(date): Sync had errors (see above)" >> "$LOG"
fi
