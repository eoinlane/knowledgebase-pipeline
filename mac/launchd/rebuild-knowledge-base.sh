#!/bin/bash
# Full knowledge base rebuild pipeline:
# 1. Export Apple Calendar events to /tmp/cal_*.txt
# 2. Build markdown KB from notes CSV + calendar data
# 3. Rsync KB to Ubuntu
# 4. Upload KB files to Open WebUI

LOG="/Users/eoin/.local/bin/rebuild-knowledge-base.log"
UBUNTU="eoin@nvidiaubuntubox"

# Sleep protection: Mac is mobile (M3, often on the move). If launchd fires
# this at 04:00 while the Mac is going to sleep, the script can be killed
# mid-flight without leaving a log entry. caffeinate -i prevents idle sleep
# for our runtime; the EXIT trap ensures any non-zero exit (signal, crash,
# etc.) leaves a forensic line in the log.
if [ "${CAFFEINATED:-0}" != "1" ]; then
    exec env CAFFEINATED=1 caffeinate -i "$0" "$@"
fi
trap 'rc=$?; [ "$rc" -ne 0 ] && echo "$(date "+%Y-%m-%d %H:%M:%S"): ABORT rc=$rc (line $LINENO)" >> "$LOG"' EXIT

echo "$(date): KB rebuild starting..." >> "$LOG"

# Step 1: Export calendars (non-fatal — falls back to existing cached files)
CAL_DIR="$HOME/.local/share/kb/calendars"
echo "$(date): Exporting calendars..." >> "$LOG"
/bin/bash /Users/eoin/.local/bin/export-calendars.sh >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    if ls "$CAL_DIR/cal_eoinlane.txt" "$CAL_DIR/cal_nta.txt" > /dev/null 2>&1; then
        cal_age=$(( ( $(date +%s) - $(date -r "$CAL_DIR/cal_nta.txt" +%s) ) / 3600 ))
        echo "$(date): Calendar export failed — using cached files (${cal_age}h old)" >> "$LOG"
    else
        echo "$(date): Calendar export failed and no cached files available — continuing without calendar data" >> "$LOG"
    fi
fi

# Step 2: Build KB
# Wait 60s for iCloud to settle — it may be syncing overnight recordings at 4am.
sleep 60
echo "$(date): Building knowledge base..." >> "$LOG"
/usr/local/bin/python3 /Users/eoin/build_knowledge_base.py >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): build_knowledge_base.py FAILED — aborting" >> "$LOG"
    exit 1
fi

# Step 2b: Build contacts DB + graph
echo "$(date): Building contacts DB..." >> "$LOG"
/usr/local/bin/python3 /Users/eoin/knowledgebase-pipeline/mac/build_contacts_db.py >> "$LOG" 2>&1

# Step 2c: LLM judgment on new merge_suggestions. Bumped 50→200/night on
# 2026-05-24 to drain the 1,267-people-files backlog faster (~20 new/week
# growth means 50/night barely kept pace). Safe to skip if LiteLLM is
# unreachable. Haiku at $0.0005/call → ~$0.10/night even at the cap.
echo "$(date): Running entity_resolver_agent (limit 200)..." >> "$LOG"
/usr/local/bin/python3 /Users/eoin/knowledgebase-pipeline/mac/entity_resolver_agent.py --limit 200 >> "$LOG" 2>&1 || \
    echo "$(date): entity_resolver_agent skipped/failed (continuing)" >> "$LOG"

echo "$(date): Building graph..." >> "$LOG"
/usr/local/bin/python3 /Users/eoin/knowledgebase-pipeline/mac/build_graph.py >> "$LOG" 2>&1

# Step 3: Rsync to Ubuntu
echo "$(date): Syncing to Ubuntu..." >> "$LOG"
rsync -az --delete \
    -e "ssh -o StrictHostKeyChecking=no" \
    /Users/eoin/knowledge_base/ "$UBUNTU:/home/eoin/knowledge_base/" >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): rsync FAILED — aborting" >> "$LOG"
    exit 1
fi

# Step 4: Cold-start voice enrolment. For each KB meeting with exactly 2
# calendar attendees (Eoin + X), if X isn't yet in voice_catalog.json AND the
# corresponding recording has exactly 2 SPEAKER clusters with one matching
# Eoin, enrol X using the unmatched embedding. Runs Ubuntu-side because that's
# where the embeddings + catalog live (KB was just rsynced over above).
# Sibling to identify_speakers.py's auto_enrol() which only extends already-
# known voices. Idempotent — safe to run every night.
echo "$(date): Running cold-start voice enrolment..." >> "$LOG"
ssh -o StrictHostKeyChecking=no "$UBUNTU" \
    "source ~/whisper-env/bin/activate && python3 ~/auto_enrol_1on1.py" >> "$LOG" 2>&1 || \
    echo "$(date): auto_enrol_1on1 skipped/failed (non-fatal)" >> "$LOG"

# Step 5: Refresh memory symlinks across project folders. Auto-discovers any
# new folder under ~/Documents containing a CLAUDE.md and re-creates per-file
# symlinks for the cross-cutting memory subset. Idempotent and silent on
# success.
echo "$(date): Refreshing memory symlinks..." >> "$LOG"
/bin/bash /Users/eoin/knowledgebase-pipeline/mac/setup-memory-symlinks.sh >> "$LOG" 2>&1 || \
    echo "$(date): memory-symlinks refresh FAILED (non-fatal)" >> "$LOG"

echo "$(date): KB rebuild complete" >> "$LOG"
