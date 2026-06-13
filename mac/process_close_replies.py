#!/usr/bin/env python3
"""process_close_replies.py

Polls Gmail for unread self-sent messages with subject "close <id>" and
runs `query_graph.py done <id>` for each. Closes the loop on the morning
brief's close-by-email links so the action-item backlog stops growing.

Auth: same Gmail app password as morning_brief_emailer.py (keychain
service=morning-brief-smtp, account=eoinlane@gmail.com). Gmail's app
passwords cover SMTP and IMAP.

Triggered by launchd every 15 minutes. Cap of MAX_PER_RUN messages per
invocation as a runaway guard.
"""

import imaplib
import re
import subprocess
import sys
from email import message_from_bytes
from pathlib import Path

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
ACCOUNT = "eoinlane@gmail.com"
KEYCHAIN_SERVICE = "morning-brief-smtp"
QUERY_GRAPH = Path.home() / "query_graph.py"
SUBJECT_RE = re.compile(r"^\s*close\s+(\d+)\s*$", re.IGNORECASE)
MAX_PER_RUN = 50


def get_password() -> str:
    r = subprocess.run(
        ["security", "find-generic-password",
         "-s", KEYCHAIN_SERVICE, "-a", ACCOUNT, "-w"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def close_item(item_id: int) -> tuple[bool, str]:
    """Shell out to query_graph.py done <id>. Returns (ok, message)."""
    r = subprocess.run(
        ["/usr/local/bin/python3", str(QUERY_GRAPH), "done", str(item_id)],
        capture_output=True, text=True, timeout=30,
    )
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    return (r.returncode == 0, out[:200])


def main() -> int:
    try:
        password = get_password()
    except subprocess.CalledProcessError as e:
        print(f"keychain read failed: {e.stderr}", file=sys.stderr)
        return 2

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(ACCOUNT, password)
    except (imaplib.IMAP4.error, OSError) as e:
        print(f"imap login failed: {e}", file=sys.stderr)
        return 3

    try:
        imap.select("INBOX")
        # Self-sent close commands only. The FROM filter is the auth boundary
        # — Gmail rejects DMARC-failing spoofs from @gmail.com itself.
        status, data = imap.search(None,
            '(UNSEEN FROM "{a}" SUBJECT "close")'.format(a=ACCOUNT))
        if status != "OK":
            print(f"search failed: {status}", file=sys.stderr)
            return 4

        nums = data[0].split() if data and data[0] else []
        if not nums:
            print("no close-reply messages")
            return 0

        processed = 0
        for num in nums[:MAX_PER_RUN]:
            status, msg_data = imap.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = message_from_bytes(msg_data[0][1])
            subject = (msg.get("Subject") or "").strip()
            m = SUBJECT_RE.match(subject)
            if not m:
                # Subject contains "close" but not "close <id>" — leave it
                # untouched so it shows up unread for human review.
                continue
            item_id = int(m.group(1))
            ok, out = close_item(item_id)
            print(f"#{item_id}: {'ok' if ok else 'FAIL'} — {out}")
            if ok:
                imap.store(num, "+FLAGS", "\\Seen")
                processed += 1

        print(f"processed {processed} close request(s)")
    finally:
        try:
            imap.close()
        except imaplib.IMAP4.error:
            pass
        imap.logout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
