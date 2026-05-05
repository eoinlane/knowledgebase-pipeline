#!/bin/bash
# Syncs new .m4a files from iCloud "My Notes Audio" to Ubuntu for transcription.
# Copies to /tmp first to avoid iCloud mmap locking (EDEADLK), then rsyncs to Ubuntu.
# Runs via launchd every 5 minutes.
#
# History: original used `cat src > dst` thinking that bypassed fcopyfile's
# mmap-based clone path. But `cat` uses read(2), which hits the SAME EDEADLK
# from iCloud's locking when a file is actively syncing. As of 2026-05-05,
# replaced with `cp` (uses APFS copyfile/clone) plus a retry loop for transient
# locks. The 11 May 2026 11:00 Ashish meeting recording was silently skipped
# for ~45 min by the cat-based version before manual intervention surfaced
# the bug.

AUDIO_DIR="/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes Audio"
TMP_DIR="/tmp/notes-audio-sync"
UBUNTU_HOST="eoin@nvidiaubuntubox"
UBUNTU_DIR="/home/eoin/audio-inbox/Notes"
LOG="/Users/eoin/.local/bin/sync-notes-audio.log"

# Sleep protection (Mac is mobile, see backup-voice-state.sh for context)
if [ "${CAFFEINATED:-0}" != "1" ]; then
    exec env CAFFEINATED=1 caffeinate -i "$0" "$@"
fi
trap 'rc=$?; [ "$rc" -ne 0 ] && echo "$(date "+%Y-%m-%d %H:%M:%S"): ABORT rc=$rc (line $LINENO)" >> "$LOG"' EXIT

echo "$(date): Sync starting..." >> "$LOG"

# Step 1: Copy new .m4a files to /tmp using cp + retry
mkdir -p "$TMP_DIR"
find "$AUDIO_DIR" -name "*.m4a" | while read -r src; do
    fname=$(basename "$src")
    dst="$TMP_DIR/$fname"
    if [ -f "$dst" ]; then
        continue
    fi
    # Try cp (APFS clone/copyfile path) up to 3 times on transient EDEADLK.
    # iCloud's lock is held while a file is actively syncing — usually clears
    # within seconds. Without retry, a single deadlock skips the file
    # indefinitely (until iCloud finishes syncing AND the next 5-min run).
    for attempt in 1 2 3; do
        if cp "$src" "$dst" 2>>"$LOG"; then
            echo "$(date): Copied $fname to /tmp" >> "$LOG"
            break
        fi
        rm -f "$dst"
        if [ "$attempt" -lt 3 ]; then
            sleep $((attempt * 5))
        else
            echo "$(date): FAILED $fname after 3 retries (likely iCloud still syncing — will retry next cycle)" >> "$LOG"
        fi
    done
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
