#!/usr/bin/env python3
"""
Reclassify a transcript based on voice-identified speakers.
Runs after speaker ID — if we know who's speaking, we know the category.

Usage: python3 reclassify_by_speaker.py <transcript_txt> <csv_path>
       python3 reclassify_by_speaker.py --batch <csv_path>  # reclassify all

The speaker→category mapping is the source of truth. If all non-Eoin
speakers belong to one category, override the LLM classification.
"""

import sys, os, re, csv, json

# Person → primary category mapping
# This is the ground truth: if this person is in a meeting, it's this category.
PERSON_CATEGORY = {
    # NTA
    "Cathal Bellew": "NTA",
    "Cathal Murphy": "NTA",
    "Declan Sheehan": "NTA",
    "Alex McKenzie": "NTA",
    "Philip L'Estrange": "NTA",
    "Ger Regan": "NTA",
    "Neil Sutch": "NTA",
    "Audrey": "NTA",
    "Gary White": "NTA",
    "Dominic Hannigan": "NTA",
    "Hugh": "NTA",
    "Mark McDermott": "NTA",

    # DCC
    "Christopher Kelly": "DCC",
    "Richie Shakespeare": "DCC",
    "Shunyu Ji": "DCC",
    "Tom Curran": "DCC",
    "Jamie Cudden": "DCC",
    "Stephen Rigney": "DCC",

    # ADAPT (overlaps with DCC — these people are ADAPT-embedded-at-DCC)
    "Declan McKibben": "ADAPT",
    "Khizer Ahmed Biyabani": "ADAPT",

    # Diotima
    "Siobhan Ryan": "Diotima",
    "Jonathan Dempsey": "Diotima",
    "Mahsa Mahdinejad": "Diotima",
    "Tom Pollock": "Diotima",
    "Rob Howell": "Diotima",

    # Paradigm
    "Guy Rackham": "Paradigm",
    "Arijit Sircar": "Paradigm",
    "Sarah Broderick": "Paradigm",
    "Eddy Moretti": "Paradigm",

    # TBS
    "Kisito Futonge Nzembayie": "TBS",
    "Daniel Coughlan": "TBS",
}

# Categories that shouldn't be overridden (personal, conference, etc.)
KEEP_CATEGORIES = {"other:personal", "other:conference", "other:lgma", "other:blank"}


def get_speakers_from_transcript(txt_path):
    """Extract speaker names from transcript [Name] labels.
    Only returns high-confidence identifications (no ? marker).
    Names with ? are LLM guesses — too unreliable for reclassification."""
    with open(txt_path) as f:
        content = f.read()
    names = set()
    for m in re.finditer(r'\[([^\]]+)\]', content):
        name = m.group(1)
        if name.startswith("SPEAKER_") or name == "UNKNOWN":
            continue
        # Skip low-confidence LLM guesses (marked with ?)
        if name.endswith("?"):
            continue
        name = name.strip()
        if name and name != "Eoin Lane":
            names.add(name)
    return names


def infer_category(speakers):
    """Given a set of speaker names, infer the meeting category.
    Returns (category, confidence) or (None, None) if ambiguous."""
    categories = set()
    matched_speakers = []

    for speaker in speakers:
        cat = PERSON_CATEGORY.get(speaker)
        if cat:
            categories.add(cat)
            matched_speakers.append((speaker, cat))

    if not categories:
        return None, None, []

    if len(categories) == 1:
        return categories.pop(), "high", matched_speakers

    # Multiple categories — check if one dominates
    # ADAPT + DCC is common (ADAPT embedded at DCC) → use DCC
    if categories == {"ADAPT", "DCC"}:
        return "DCC", "medium", matched_speakers

    # Otherwise ambiguous
    return None, None, matched_speakers


def reclassify(txt_path, csv_path, dry_run=False):
    """Check if a transcript should be reclassified based on speakers."""
    speakers = get_speakers_from_transcript(txt_path)
    if not speakers:
        return None

    # Get UUID from transcript
    with open(txt_path) as f:
        for line in f:
            if line.startswith("File:"):
                uuid = line.replace("File:", "").strip()
                uuid = re.sub(r'\.(m4a|txt)$', '', uuid)
                break
        else:
            return None

    # Get current classification
    current_cat = None
    csv_row_idx = None
    rows = []
    with open(csv_path) as f:
        for i, row in enumerate(csv.reader(f)):
            rows.append(row)
            if len(row) >= 3 and uuid in row[0]:
                current_cat = row[2]
                csv_row_idx = i

    if current_cat is None or csv_row_idx is None:
        return None

    # Don't override personal/conference/blank
    if current_cat in KEEP_CATEGORIES:
        return None

    new_cat, confidence, matched = infer_category(speakers)
    if not new_cat or new_cat == current_cat:
        return None

    if dry_run:
        return {
            "uuid": uuid[:8],
            "current": current_cat,
            "proposed": new_cat,
            "confidence": confidence,
            "speakers": [(s, c) for s, c in matched],
        }

    # Update CSV
    rows[csv_row_idx][2] = new_cat
    with open(csv_path, "w", newline="") as f:
        csv.writer(f).writerows(rows)

    return {
        "uuid": uuid[:8],
        "current": current_cat,
        "new": new_cat,
        "confidence": confidence,
        "speakers": [(s, c) for s, c in matched],
    }


def batch_reclassify(csv_path, dry_run=False):
    """Scan all transcripts and reclassify based on speakers."""
    trans_dir = os.path.expanduser("~/audio-inbox/Transcriptions")
    changes = []

    for fname in sorted(os.listdir(trans_dir)):
        if not fname.endswith(".txt"):
            continue
        txt_path = os.path.join(trans_dir, fname)
        result = reclassify(txt_path, csv_path, dry_run=dry_run)
        if result:
            changes.append(result)

    prefix = "WOULD CHANGE" if dry_run else "CHANGED"
    print(f"\n{prefix}: {len(changes)} recordings\n")
    for c in changes:
        speakers = ", ".join(f"{s}→{cat}" for s, cat in c.get("speakers", []))
        old = c.get("current", "?")
        new = c.get("proposed" if dry_run else "new", "?")
        print(f"  {c['uuid']} {old} → {new} ({c['confidence']}) [{speakers}]")

    return changes


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 reclassify_by_speaker.py <transcript.txt> <csv_path>")
        print("       python3 reclassify_by_speaker.py --batch <csv_path> [--dry-run]")
        sys.exit(1)

    if sys.argv[1] == "--batch":
        csv_path = sys.argv[2]
        dry_run = "--dry-run" in sys.argv
        batch_reclassify(csv_path, dry_run=dry_run)
    else:
        txt_path = sys.argv[1]
        csv_path = sys.argv[2]
        result = reclassify(txt_path, csv_path)
        if result:
            print(f"Reclassified: {result}")
        else:
            print("No change needed")
