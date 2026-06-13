#!/bin/bash
# Process close-by-email replies. IMAPs Gmail for unread self-sent messages
# with subject "close <id>" and runs query_graph.py done <id> for each.
# Runs every 15 minutes via launchd (com.eoin.process-close-replies).

LOG="/Users/eoin/.local/bin/process-close-replies.log"
SCRIPT="/Users/eoin/process_close_replies.py"

echo "$(date): === process-close-replies START ===" >> "$LOG"
/usr/local/bin/python3 "$SCRIPT" >> "$LOG" 2>&1
echo "$(date): === process-close-replies END ===" >> "$LOG"
exit 0
