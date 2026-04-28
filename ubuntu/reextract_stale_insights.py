#!/usr/bin/env python3
"""
Find recordings where the speaker mappings have been updated since the
insights JSON was last written — i.e. action items/decisions extracted
under stale labels. Re-extracts them.

Comparison is by file mtime: speaker_mappings.json is per-recording (the
whole file is rewritten on any change), so we use the per-UUID block's
last update OR fall back to the top-level mtime. Insights JSON is
per-recording — its mtime captures when extraction last ran.

Usage:
    reextract_stale_insights.py            # dry-run (list candidates)
    reextract_stale_insights.py --apply    # actually re-extract
    reextract_stale_insights.py --limit N  # cap (default 20)
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

MAPPINGS = Path.home() / "speaker_mappings.json"
INSIGHTS_DIR = Path.home() / "audio-inbox" / "Insights"
TRANS_DIR = Path.home() / "audio-inbox" / "Transcriptions"
CSV = Path.home() / "audio-inbox" / "classification.csv"
EXTRACTOR = Path.home() / "extract_meeting_insights.py"


def stale_uuids():
    """Find recordings whose per-UUID `mappings_updated_at` is more recent
    than the insights JSON's mtime. UUIDs without a stamp are skipped —
    use --backfill to seed them with the file mtime as a baseline."""
    from datetime import datetime
    if not MAPPINGS.exists():
        return []
    mappings = json.loads(MAPPINGS.read_text())
    candidates = []
    for uuid, rec in mappings.items():
        if not isinstance(rec, dict):
            continue
        ins = INSIGHTS_DIR / f"{uuid}.json"
        if not ins.exists():
            continue
        per_uuid_ts = rec.get("mappings_updated_at")
        if not per_uuid_ts:
            continue  # no baseline yet — wait for next mapping change
        try:
            dt = datetime.fromisoformat(per_uuid_ts.replace("Z", ""))
            ts = dt.timestamp()
        except Exception:
            continue
        ins_mtime = ins.stat().st_mtime
        if ts > ins_mtime:
            candidates.append((uuid, ts - ins_mtime))
    candidates.sort(key=lambda x: -x[1])
    return candidates


def backfill_stamps():
    """One-shot migration: stamp every existing UUID block with a baseline
    `mappings_updated_at` so the detector has a starting point.

    Baseline is the *insights file mtime* when one exists — the assumption
    is that when insights were extracted, the mapping in effect at that
    moment was current, so stamp == insights mtime means "in sync". This
    guarantees the detector emits 0 candidates immediately after backfill,
    and only flags UUIDs whose mapping is *later* changed.

    For UUIDs without an insights file, fall back to the mappings file
    mtime — irrelevant for staleness detection (the detector requires the
    insights file to exist).
    """
    from datetime import datetime
    if not MAPPINGS.exists():
        return 0
    mappings = json.loads(MAPPINGS.read_text())
    fallback = datetime.fromtimestamp(MAPPINGS.stat().st_mtime).isoformat(timespec="seconds")
    n = 0
    for uuid, rec in mappings.items():
        if not isinstance(rec, dict) or "mappings_updated_at" in rec:
            continue
        ins = INSIGHTS_DIR / f"{uuid}.json"
        if ins.exists():
            ts = datetime.fromtimestamp(ins.stat().st_mtime).isoformat(timespec="seconds")
        else:
            ts = fallback
        rec["mappings_updated_at"] = ts
        n += 1
    if n:
        MAPPINGS.write_text(json.dumps(mappings, indent=2))
    return n


def reextract(uuid):
    txt = TRANS_DIR / f"{uuid}.txt"
    if not txt.exists():
        return False, "transcript missing"
    try:
        r = subprocess.run(
            ["python3", str(EXTRACTOR), str(txt), str(CSV)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode == 0:
            for line in reversed(r.stdout.strip().splitlines()):
                if line.strip():
                    return True, line[:160]
            return True, "ok"
        return False, f"exit {r.returncode}: {r.stderr.strip()[:200]}"
    except Exception as e:
        return False, f"error: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--backfill", action="store_true",
                    help="One-shot: stamp existing UUIDs with a baseline timestamp")
    args = ap.parse_args()

    if args.backfill:
        n = backfill_stamps()
        print(f"Backfilled {n} UUID(s) with baseline mappings_updated_at.")
        return

    cands = stale_uuids()
    print(f"Stale insights candidates: {len(cands)}")
    if not cands:
        return

    cands = cands[:args.limit]
    for uuid, lag in cands:
        hours = lag / 3600
        print(f"  {uuid}  ({hours:.1f}h stale)")
    if not args.apply:
        print("\nDry-run. Use --apply to re-extract.")
        return

    print(f"\nRe-extracting {len(cands)} insights file(s)...")
    ok = err = 0
    for uuid, _ in cands:
        success, msg = reextract(uuid)
        marker = "✓" if success else "✗"
        print(f"  {marker} {uuid[:8]}: {msg}")
        if success:
            ok += 1
        else:
            err += 1
    print(f"\nDone. {ok} succeeded, {err} failed.")


if __name__ == "__main__":
    main()
