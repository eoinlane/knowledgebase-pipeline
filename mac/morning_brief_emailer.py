#!/usr/bin/env python3
"""morning_brief_emailer.py

Sends ~/morning_brief.md to eoinlane@gmail.com via Gmail SMTP. App password is
read from the macOS login keychain (service=morning-brief-smtp, account=
eoinlane@gmail.com). Designed to be called from morning-brief.sh after the
brief file is written.

Failure mode: if SMTP, keychain, or file access fail, we print the error to
stderr and exit non-zero — but morning-brief.sh treats this as non-fatal so
the markdown file is still produced regardless.

Run manually:
  python3 morning_brief_emailer.py
"""

import argparse
import datetime as dt
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

BRIEF_PATH_DEFAULT = Path.home() / "morning_brief.md"
SUBJECT_DEFAULT = "Morning Brief"
RECIPIENT = "eoinlane@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
KEYCHAIN_SERVICE = "morning-brief-smtp"
KEYCHAIN_ACCOUNT = "eoinlane@gmail.com"


def get_app_password() -> str:
    result = subprocess.run(
        ["security", "find-generic-password",
         "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def markdown_to_html(md: str) -> str:
    """Tiny markdown-to-HTML for the brief's known structure.
    Handles: # / ## / ### headers, - list items, bold **x**, plain paragraphs.
    Not general-purpose; tuned to query_graph.py brief output."""
    out = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if line.startswith("# "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h2>{line[2:]}</h2>")
        elif line.startswith("## "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("### "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h4>{line[4:]}</h4>")
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            item = line[2:]
            # bold conversion
            while "**" in item:
                item = item.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
            out.append(f"<li>{item}</li>")
        elif line == "---":
            if in_list:
                out.append("</ul>"); in_list = False
            out.append("<hr>")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def strip_frontmatter(md: str) -> str:
    """Drop the YAML frontmatter block at the top of the brief."""
    lines = md.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i+1:]).lstrip()
    return md


def main() -> int:
    parser = argparse.ArgumentParser(description="Email a markdown file via Gmail SMTP")
    parser.add_argument("--file", type=Path, default=BRIEF_PATH_DEFAULT,
                        help=f"Markdown file to send (default: {BRIEF_PATH_DEFAULT})")
    parser.add_argument("--subject", default=SUBJECT_DEFAULT,
                        help=f"Email subject prefix; date appended (default: '{SUBJECT_DEFAULT}')")
    args = parser.parse_args()

    src = args.file
    if not src.exists():
        print(f"File not found at {src}", file=sys.stderr)
        return 2

    md = src.read_text()
    body_md = strip_frontmatter(md)

    today_str = dt.date.today().strftime("%A %d %B %Y")
    subject = f"{args.subject} — {today_str}"

    msg = EmailMessage()
    msg["From"] = RECIPIENT
    msg["To"] = RECIPIENT
    msg["Subject"] = subject
    msg.set_content(body_md)
    msg.add_alternative(markdown_to_html(body_md), subtype="html")

    try:
        password = get_app_password()
    except subprocess.CalledProcessError as e:
        print(f"Keychain read failed: {e.stderr}", file=sys.stderr)
        return 3

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(RECIPIENT, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        print(f"SMTP send failed: {e}", file=sys.stderr)
        return 4

    print(f"Sent: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
