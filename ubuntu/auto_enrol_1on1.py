#!/usr/bin/env python3
"""Cold-start voice catalog enrolment for 1-on-1 calls.

Sibling to the existing `auto_enrol()` in identify_speakers.py — that one
*extends* existing catalog entries by appending samples when a recording
matches a known voice with ≥0.92 similarity. It cannot add a brand-new
person to the catalog. This script fills that gap.

Logic: when a KB meeting file has **exactly 2 calendar attendees**
(Eoin + X) and the corresponding recording's embeddings have **exactly
2 SPEAKER clusters**, the unknown speaker IS X by elimination — calendar
is canon. Conservative gating prevents misattribution:

  - Eoin must match one cluster with similarity ≥ 0.65 (high confidence)
  - The OTHER cluster must have NO existing-catalog match ≥ 0.55
    (avoids stealing a voice that's already catalogued under a different name)
  - X must not already be in the catalog (avoids duplicate entries)

Run on Ubuntu after the nightly KB build, so the calendar matching has
already populated `attendees:` in each KB meeting file. Idempotent —
safe to re-run; already-enrolled people are skipped.

Wire into `mac/launchd/rebuild-knowledge-base.sh` after the rsync-back step.
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path
import numpy as np

KB_DIR = Path(os.path.expanduser("~/knowledge_base/meetings"))
CATALOG = Path(os.path.expanduser("~/voice_catalog.json"))
EMBEDDINGS_DIR = Path(os.path.expanduser("~/audio-inbox/Embeddings"))

EOIN_MIN_SIM = 0.65    # Eoin must be matched confidently
OTHER_MAX_SIM = 0.55   # other speaker must NOT already exist in catalog


def cosine(a, b):
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def atomic_write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def parse_kb_attendees(kb_text):
    """Parse the `attendees:` YAML-like list from KB frontmatter. Returns list
    of attendee names, stripping emails and noise."""
    m = re.search(r'^attendees:\s*\[(.+?)\]\s*$', kb_text, re.MULTILINE)
    if not m:
        return []
    raw = m.group(1)
    names = [a.strip().strip('"\'') for a in raw.split(",")]
    return [n for n in names if n and "@" not in n]


def find_candidates(kb_dir):
    """Yield (kb_filename, source_file_uuid, other_attendee) for each KB
    meeting with exactly 2 calendar attendees (Eoin + one other)."""
    if not kb_dir.is_dir():
        return
    for f in sorted(kb_dir.glob("*.md")):
        text = f.read_text(errors="replace")
        attendees = parse_kb_attendees(text)
        # Always include Eoin as recorder — he's not always in the calendar list
        if not any("eoin" in a.lower() for a in attendees):
            attendees = ["Eoin Lane"] + attendees
        if len(attendees) != 2:
            continue
        eoin = next((a for a in attendees if "eoin" in a.lower()), None)
        other = next((a for a in attendees if a != eoin), None)
        if not eoin or not other:
            continue
        sm = re.search(r'^source_file:\s*(\S+)', text, re.MULTILINE)
        if not sm:
            continue
        yield f.name, sm.group(1), other


def best_catalog_match(emb, catalog, exclude=None):
    """Return (best_name, best_sim) for emb against catalog, ignoring `exclude`."""
    best_name, best_sim = None, 0.0
    for name, data in catalog.items():
        if name == exclude:
            continue
        for sample in data.get("embeddings", []):
            s = cosine(emb, sample)
            if s > best_sim:
                best_name, best_sim = name, s
    return best_name, best_sim


def main():
    if not CATALOG.exists():
        print(f"FAIL: catalog not found at {CATALOG}", file=sys.stderr)
        sys.exit(1)
    catalog = json.load(open(CATALOG))
    eoin_embs = catalog.get("Eoin Lane", {}).get("embeddings", [])
    if not eoin_embs:
        print("FAIL: Eoin Lane not in catalog — cannot cold-start enrol without an Eoin anchor", file=sys.stderr)
        sys.exit(1)

    candidates = list(find_candidates(KB_DIR))
    print(f"Scanning {len(candidates)} KB meetings with exactly 2 calendar attendees...")
    print()

    enrolled = []
    skipped_reasons = {}
    for kb_file, uuid, other in candidates:
        # Already in catalog → skip
        if other in catalog:
            skipped_reasons[other] = skipped_reasons.get(other, 0) + 1
            continue

        emb_path = EMBEDDINGS_DIR / f"{uuid}.json"
        if not emb_path.exists():
            continue
        try:
            embs = json.load(open(emb_path))
        except Exception:
            continue
        speakers = list(embs.keys())
        if len(speakers) != 2:
            continue

        s0_emb = embs[speakers[0]]["embedding"]
        s1_emb = embs[speakers[1]]["embedding"]
        s0_eoin = max(cosine(s0_emb, e) for e in eoin_embs)
        s1_eoin = max(cosine(s1_emb, e) for e in eoin_embs)

        # Decide which speaker is Eoin
        if s0_eoin >= EOIN_MIN_SIM and s0_eoin > s1_eoin:
            other_emb = s1_emb
            other_label = speakers[1]
        elif s1_eoin >= EOIN_MIN_SIM and s1_eoin > s0_eoin:
            other_emb = s0_emb
            other_label = speakers[0]
        else:
            continue  # Eoin not confidently matched in either cluster

        # The OTHER cluster must not already match someone in catalog
        existing_name, existing_sim = best_catalog_match(other_emb, catalog, exclude="Eoin Lane")
        if existing_sim >= OTHER_MAX_SIM:
            # Already known under a different name (or matches someone strongly) — skip
            print(f"  [skip] {kb_file}: other voice already matches {existing_name} (sim={existing_sim:.3f})")
            continue

        # Enrol
        catalog[other] = {"embeddings": [other_emb]}
        enrolled.append((kb_file, uuid, other_label, other))
        print(f"  ✓ {kb_file}: {other_label} → {other}")

    if enrolled:
        atomic_write_json(str(CATALOG), catalog)
        total = sum(len(v.get("embeddings", [])) for v in catalog.values())
        print()
        print(f"✓ Enrolled {len(enrolled)} new voice(s). Catalog now {len(catalog)} people, {total} embeddings.")
    else:
        print()
        print("(no new enrolments — no qualifying candidates)")

    if skipped_reasons:
        already = sum(skipped_reasons.values())
        print(f"  ({already} candidate(s) skipped — name already in catalog)")


if __name__ == "__main__":
    main()
