#!/bin/bash
# Nightly backup of Ubuntu voice-state JSONs to Mac. These files have no
# upstream and are mutated frequently by identify_speakers / review_speakers
# / batch_identify; a disk failure or torn write on Ubuntu would otherwise
# permanently destroy 29+ enrolled voice fingerprints.
#
# Backups land in ~/.local/share/kb/backups/voice/YYYY-MM-DD/ — keep last 30
# days, then prune. Logs to ~/.local/bin/backup-voice-state.log.

set -euo pipefail

LOG="$HOME/.local/bin/backup-voice-state.log"
ROOT="$HOME/.local/share/kb/backups/voice"
UBUNTU="eoin@nvidiaubuntubox"
TODAY=$(date +%Y-%m-%d)
DEST="$ROOT/$TODAY"

# Keep Mac awake for the whole run. The 2026-05-04 03:23 backup produced an
# empty directory because the Mac was asleep at 03:23, woke briefly at 03:29
# when launchd deferred the job, but went back to sleep mid-SSH before the
# script could log anything. caffeinate -i prevents idle sleep for the
# duration of this process.
if [ "${CAFFEINATED:-0}" != "1" ]; then
    exec env CAFFEINATED=1 caffeinate -i "$0" "$@"
fi

# Always log SOMETHING on exit so silent failures are visible. Trap fires on
# any exit (success, error, signal) — if we exit before the success line at
# the bottom, this still leaves a forensic trace.
trap 'rc=$?; [ "$rc" -ne 0 ] && echo "$(date "+%Y-%m-%d %H:%M:%S"): ABORT rc=$rc (line $LINENO)" >> "$LOG"' EXIT

mkdir -p "$DEST"
ts=$(date "+%Y-%m-%d %H:%M:%S")

# Pull each file individually so a missing one doesn't fail the others.
# voice_catalog.json is the most critical (full embeddings); the others
# (mappings/registry) are lighter but also costly to rebuild.
# --ignore-missing-args silently no-ops when the source file doesn't exist
# (e.g. speaker_registry.json before the first review_speakers harvest)
# instead of logging an rsync warning every night.
ok=0
fail=0
missing=0
for f in voice_catalog.json speaker_mappings.json speaker_registry.json; do
    # Pre-check existence — retry up to 3× on SSH flakiness. A transient SSH
    # connection glitch at 03:25 AM on 2026-05-01 caused voice_catalog.json
    # to be falsely reported "missing" on a single retry. The retry loop
    # distinguishes SSH errors (return code other than 0 or 1) from genuine
    # file-not-found.
    found=""
    for attempt in 1 2 3; do
        out=$(ssh -o ConnectTimeout=15 -o BatchMode=yes "$UBUNTU" "test -f ~/$f && echo exists || echo notfound" 2>/dev/null)
        case "$out" in
            exists) found=1; break ;;
            notfound) found=0; break ;;
            *) sleep $((attempt * 2)) ;;  # SSH error — back off and retry
        esac
    done
    if [ "$found" = "0" ]; then
        missing=$((missing+1))
        continue
    elif [ -z "$found" ]; then
        echo "$(date): SSH unreachable after 3 retries for $f — skipping" >>"$LOG"
        fail=$((fail+1))
        continue
    fi
    if rsync -az --timeout=30 \
        -e "ssh -o ConnectTimeout=15 -o BatchMode=yes" \
        "$UBUNTU:~/$f" "$DEST/$f" 2>>"$LOG"; then
        ok=$((ok+1))
    else
        fail=$((fail+1))
    fi
done

# Sanity check: voice_catalog.json must be valid JSON and non-trivial.
# A 0-byte file from a failed atomic write would be caught here.
if [ -f "$DEST/voice_catalog.json" ]; then
    if ! /usr/bin/python3 -c "import json,sys; d=json.load(open('$DEST/voice_catalog.json')); sys.exit(0 if isinstance(d, dict) and len(d) >= 5 else 1)" 2>>"$LOG"; then
        echo "$ts: WARNING — voice_catalog.json invalid or has <5 people; not pruning" >> "$LOG"
        echo "$ts: ok=$ok fail=$fail (sanity FAILED)" >> "$LOG"
        exit 1
    fi
fi

# Prune backups older than 30 days
find "$ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + 2>>"$LOG" || true

# Compact log line on success
size=$(du -sh "$DEST" 2>/dev/null | cut -f1)
extra=""
[ "$missing" -gt 0 ] && extra=" (${missing} not yet on Ubuntu)"
echo "$ts: OK — backed up to $DEST ($size, $ok files)${extra}" >> "$LOG"
