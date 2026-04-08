#!/bin/bash
# Backup Claude memory files to Ubuntu whenever they change.

LOG="/Users/eoin/.local/bin/backup-claude-memory.log"
SRC="/Users/eoin/.claude/projects/"
DEST="eoin@nvidiaubuntubox:~/claude-memory-backup/"

echo "$(date): Starting Claude memory backup..." >> "$LOG"

if rsync -a --delete -e "ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no" \
    "$SRC" "$DEST" >> "$LOG" 2>&1; then
    echo "$(date): Backup complete." >> "$LOG"
else
    echo "$(date): Backup FAILED." >> "$LOG"
fi
