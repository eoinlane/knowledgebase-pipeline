#!/bin/bash
# Full knowledge base rebuild pipeline:
# 1. Export Apple Calendar events to /tmp/cal_*.txt
# 2. Build markdown KB from notes CSV + calendar data
# 3. Rsync KB to Ubuntu
# 4. Upload KB files to Open WebUI

LOG="/Users/eoin/.local/bin/rebuild-knowledge-base.log"
UBUNTU="eoin@nvidiaubuntubox"
echo "$(date): KB rebuild starting..." >> "$LOG"

# Step 1: Export calendars (non-fatal — falls back to existing /tmp/ files)
echo "$(date): Exporting calendars..." >> "$LOG"
/bin/bash /Users/eoin/.local/bin/export-calendars.sh >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    # Check if we have usable cal files from a previous run
    if ls /tmp/cal_eoinlane.txt /tmp/cal_nta.txt > /dev/null 2>&1; then
        cal_age=$(( ( $(date +%s) - $(date -r /tmp/cal_nta.txt +%s) ) / 3600 ))
        echo "$(date): Calendar export failed — using existing /tmp/ files (${cal_age}h old)" >> "$LOG"
    else
        echo "$(date): Calendar export failed and no /tmp/ files available — continuing without calendar data" >> "$LOG"
    fi
fi

# Step 2: Build KB
echo "$(date): Building knowledge base..." >> "$LOG"
/usr/local/bin/python3 /Users/eoin/build_knowledge_base.py >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): build_knowledge_base.py FAILED — aborting" >> "$LOG"
    exit 1
fi

# Step 3: Rsync to Ubuntu
echo "$(date): Syncing to Ubuntu..." >> "$LOG"
rsync -az --delete \
    -e "ssh -o StrictHostKeyChecking=no" \
    /Users/eoin/knowledge_base/ "$UBUNTU:/home/eoin/knowledge_base/" >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): rsync FAILED — aborting" >> "$LOG"
    exit 1
fi

# Step 4: Upload to Open WebUI
echo "$(date): Uploading to Open WebUI..." >> "$LOG"
/usr/local/bin/python3 /Users/eoin/upload_knowledge_base.py >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date): upload FAILED" >> "$LOG"
    exit 1
fi

echo "$(date): KB rebuild complete" >> "$LOG"
