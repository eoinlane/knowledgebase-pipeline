#!/usr/bin/env python3
"""
End-of-day reconciliation report.

Snapshots today's KB meetings, runs a fresh build (with fresh calendar
export), then diffs to surface any meetings whose calendar match changed
during the day. Optionally re-runs speaker identification on changed
meetings whose mappings aren't user-confirmed.

Usage:
    eod_reconciliation.py snapshot --date YYYY-MM-DD --out PATH
    eod_reconciliation.py diff BEFORE.json AFTER.json [--reid]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date as _date, datetime
from pathlib import Path

KB_MEETINGS = Path.home() / "knowledge_base" / "meetings"
UBUNTU_HOST = "eoin@nvidiaubuntubox"
UBUNTU_TRANS_DIR = "/home/eoin/audio-inbox/Transcriptions"
UBUNTU_CSV = "/home/eoin/audio-inbox/classification.csv"


def snapshot(target_date):
    """Capture relevant frontmatter fields for every KB meeting on `target_date`.
    Keyed by source_file (UUID) for stable diffing across rebuilds (filenames
    can change if topic/category changes)."""
    snap = {}
    for f in sorted(KB_MEETINGS.glob("*.md")):
        text = f.read_text(errors="replace")
        front = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not front:
            continue
        fm = front.group(1)
        date_m = re.search(r"^date:\s*(\S+)", fm, re.MULTILINE)
        if not date_m or date_m.group(1).strip() != target_date:
            continue

        def grab(field, multiline=False):
            m = re.search(rf"^{field}:\s*(.+?)$", fm, re.MULTILINE)
            return m.group(1).strip().strip('"') if m else ""

        source_file = grab("source_file")
        if not source_file:
            continue

        # Normalise attendees / mentioned to a sorted tuple for stable comparison
        att_m = re.search(r"^attendees:\s*\[(.*?)\]", fm, re.MULTILINE | re.DOTALL)
        attendees = []
        if att_m:
            for s in re.findall(r'"([^"]+)"', att_m.group(1)):
                attendees.append(s)

        snap[source_file] = {
            "filename": f.name,
            "title": grab("title"),
            "category": grab("category"),
            "topic": grab("topic"),
            "matched_event": grab("matched_event"),
            "matched_event_score": grab("matched_event_score"),
            "matched_event_delta_min": grab("matched_event_delta_min"),
            "attendees_source": grab("attendees_source"),
            "attendees": sorted(attendees),
        }
    return snap


def diff(before, after, do_reid=False):
    """Compare two snapshots, print a markdown report."""
    print(f"# EOD Reconciliation — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print()
    all_uuids = set(before) | set(after)
    changed_uuids = []
    new_uuids = []
    removed_uuids = []
    same_uuids = []

    for uuid in sorted(all_uuids):
        b, a = before.get(uuid), after.get(uuid)
        if not b and a:
            new_uuids.append(uuid)
        elif b and not a:
            removed_uuids.append(uuid)
        else:
            diffs = {}
            for field in ("category", "matched_event", "attendees", "title", "topic"):
                if b.get(field) != a.get(field):
                    diffs[field] = (b.get(field), a.get(field))
            if diffs:
                changed_uuids.append((uuid, diffs))
            else:
                same_uuids.append(uuid)

    print(f"- Meetings unchanged: **{len(same_uuids)}**")
    print(f"- Meetings changed:   **{len(changed_uuids)}**")
    print(f"- Meetings new:       **{len(new_uuids)}**")
    print(f"- Meetings removed:   **{len(removed_uuids)}**")
    print()

    if changed_uuids:
        print("## Changed meetings\n")
        for uuid, diffs in changed_uuids:
            after_meta = after.get(uuid, {})
            print(f"### {uuid}  →  {after_meta.get('filename', '?')}")
            for field, (before_v, after_v) in diffs.items():
                if isinstance(before_v, list) or isinstance(after_v, list):
                    bset = set(before_v or [])
                    aset = set(after_v or [])
                    added = aset - bset
                    removed = bset - aset
                    if added or removed:
                        print(f"  - `{field}` changed:")
                        if added:
                            print(f"      + added: {', '.join(sorted(added))}")
                        if removed:
                            print(f"      − removed: {', '.join(sorted(removed))}")
                else:
                    print(f"  - `{field}`:  `{before_v!r}` → `{after_v!r}`")
            print()
        if do_reid:
            print("## Re-identifying speakers on changed meetings\n")
            for uuid, diffs in changed_uuids:
                if "attendees" not in diffs:
                    continue  # speaker re-id only useful when attendees changed
                if not _is_unconfirmed(uuid):
                    print(f"  {uuid[:8]}: skipped — already user-confirmed")
                    continue
                ok, msg = _trigger_speaker_id(uuid)
                marker = "✓" if ok else "✗"
                print(f"  {marker} {uuid[:8]}: {msg}")

    if new_uuids:
        print(f"## New meetings appeared in build ({len(new_uuids)})\n")
        for u in new_uuids:
            print(f"- {after[u].get('filename', u)}")
        print()


def _is_unconfirmed(uuid):
    """Check Ubuntu speaker_mappings.json — only re-id meetings the user
    hasn't already confirmed."""
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", UBUNTU_HOST,
             f"python3 -c 'import json; "
             f"d=json.load(open(\"/home/eoin/speaker_mappings.json\")); "
             f"r=d.get(\"{uuid}\", {{}}); "
             f"print(\"unconfirmed\" if not r.get(\"confirmed\") else \"confirmed\")'"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() == "unconfirmed"
    except Exception:
        return False  # err on the side of don't-touch


def _trigger_speaker_id(uuid):
    """Re-run speaker identification AND insight re-extraction. The two are
    paired: when speaker labels change, the action items / decisions that
    were extracted under the old labels are now wrong. Re-extracting
    refreshes the insights JSON so owner attribution matches the current
    speaker mapping."""
    cmd = (
        f"source ~/whisper-env/bin/activate && "
        f"python3 ~/identify_speakers.py "
        f"{UBUNTU_TRANS_DIR}/{uuid}.txt {UBUNTU_CSV} && "
        f"python3 ~/extract_meeting_insights.py "
        f"{UBUNTU_TRANS_DIR}/{uuid}.txt {UBUNTU_CSV}"
    )
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", UBUNTU_HOST, cmd],
            capture_output=True, text=True, timeout=600,
        )
        if out.returncode == 0:
            last = out.stdout.strip().splitlines()[-4:]
            return True, " | ".join(last)
        return False, f"exit {out.returncode}: {out.stderr.strip()[:200]}"
    except Exception as e:
        return False, f"error: {e}"


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("--date", required=True)
    p_snap.add_argument("--out", required=True)
    p_diff = sub.add_parser("diff")
    p_diff.add_argument("before")
    p_diff.add_argument("after")
    p_diff.add_argument("--reid", action="store_true")
    args = ap.parse_args()

    if args.cmd == "snapshot":
        snap = snapshot(args.date)
        Path(args.out).write_text(json.dumps(snap, indent=2))
        print(f"Snapshotted {len(snap)} meeting(s) on {args.date} → {args.out}")
    elif args.cmd == "diff":
        before = json.loads(Path(args.before).read_text())
        after = json.loads(Path(args.after).read_text())
        diff(before, after, do_reid=args.reid)


if __name__ == "__main__":
    main()
