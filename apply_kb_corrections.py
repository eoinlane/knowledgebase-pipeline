#!/usr/bin/env python3
"""
apply_kb_corrections.py — Patch rebuilt KB markdown files with manual corrections.
Run after build_knowledge_base.py: python3 ~/knowledgebase-pipeline/apply_kb_corrections.py
Reads: ~/kb_corrections.json
Patches: ~/knowledge_base/meetings/*.md, ~/knowledge_base/people/*.md
"""

import json
import re
import yaml
from pathlib import Path

CORRECTIONS_FILE = Path.home() / "kb_corrections.json"
KB_DIR           = Path.home() / "knowledge_base"
MEETINGS_DIR     = KB_DIR / "meetings"
PEOPLE_DIR       = KB_DIR / "people"


def load_corrections():
    if not CORRECTIONS_FILE.exists():
        return {"people": {}, "meetings": {}}
    with open(CORRECTIONS_FILE) as f:
        return json.load(f)


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def split_frontmatter(content):
    """Return (frontmatter_str, body_str) or (None, content) if no frontmatter."""
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    return parts[1], parts[2]


def patch_meeting_file(path, meeting_correction, people_corrections):
    """Apply corrections to a meeting markdown file."""
    content = read_file(path)
    fm_str, body = split_frontmatter(content)
    if fm_str is None:
        return False

    try:
        fm = yaml.safe_load(fm_str) or {}
    except yaml.YAMLError:
        return False

    changed = False

    # Topic override
    if "topic" in meeting_correction:
        new_topic = meeting_correction["topic"]
        if fm.get("topic") != new_topic:
            fm["topic"] = new_topic
            fm["title"] = new_topic
            changed = True

    # Tags (secondary categories beyond the primary)
    if "tags" in meeting_correction:
        existing_tags = fm.get("tags", [])
        new_tags = meeting_correction["tags"]
        if set(existing_tags) != set(new_tags):
            fm["tags"] = sorted(new_tags)
            changed = True

    # People corrections: rename specific names in the people list
    if people_corrections and "people" in fm:
        raw_people = fm["people"]
        # Flatten the YAML list (handles both ["A,B,C"] and ["A","B","C"] formats)
        all_names = []
        for entry in raw_people:
            if isinstance(entry, str):
                all_names.extend([n.strip() for n in entry.split(",")])
        # Apply corrections
        corrected = [people_corrections.get(n, n) for n in all_names if n]
        if corrected != all_names:
            fm["people"] = [", ".join(corrected)]
            changed = True

    if not changed:
        return False

    # Serialise back — preserve field order
    field_order = ["title", "date", "recorded", "category", "tags", "topic", "people", "source_file"]
    lines = ["---"]
    for key in field_order:
        if key not in fm:
            continue
        val = fm[key]
        if isinstance(val, list):
            if key == "people":
                inner = ", ".join(f'"{v}"' for v in val)
                lines.append(f"people: [{inner}]")
            else:
                lines.append(f"{key}: {json.dumps(val)}")
        elif isinstance(val, str):
            lines.append(f'{key}: "{val}"')
        else:
            lines.append(f"{key}: {val}")
    # Any keys not in our order
    for key, val in fm.items():
        if key not in field_order:
            lines.append(f"{key}: {val}")
    lines.append("---")

    # Also patch the ## Overview table and ## People section in the body
    if "topic" in meeting_correction:
        new_topic = meeting_correction["topic"]
        body = re.sub(
            r'(\*\*Topic\*\*\s*\|\s*).*',
            f'**Topic** | {new_topic}',
            body
        )
        body = re.sub(
            r'(\| \*\*Topic\*\* \| ).*(\|)',
            f'\\g<1>{new_topic}\\2',
            body
        )

    if people_corrections:
        for old_name, new_name in people_corrections.items():
            # Update ## People section
            body = re.sub(
                rf'\b{re.escape(old_name)}\b',
                new_name,
                body
            )

    write_file(path, "\n".join(lines) + body)
    return True


def patch_people_file(path, person_data):
    """Update name, add title/org to a people file."""
    content = read_file(path)
    fm_str, body = split_frontmatter(content)
    if fm_str is None:
        return False

    try:
        fm = yaml.safe_load(fm_str) or {}
    except yaml.YAMLError:
        return False

    changed = False
    if "name" in person_data and fm.get("name") != person_data["name"]:
        fm["name"] = person_data["name"]
        changed = True
    if "title" in person_data and fm.get("title") != person_data["title"]:
        fm["title"] = person_data["title"]
        changed = True
    if "org" in person_data and fm.get("org") != person_data["org"]:
        fm["org"] = person_data["org"]
        changed = True

    if not changed:
        return False

    lines = ["---"]
    for key in ["name", "title", "org", "meeting_count"]:
        if key in fm:
            val = fm[key]
            if isinstance(val, str):
                lines.append(f'{key}: "{val}"')
            else:
                lines.append(f"{key}: {val}")
    for key, val in fm.items():
        if key not in ("name", "title", "org", "meeting_count"):
            lines.append(f"{key}: {val}")
    lines.append("---")

    write_file(path, "\n".join(lines) + body)
    return True


def apply():
    corrections = load_corrections()
    people_corrections = corrections.get("people", {})
    meeting_corrections = corrections.get("meetings", {})

    if not people_corrections and not meeting_corrections:
        print("No corrections to apply.")
        return

    # Build name→slug lookup for people files
    # e.g. "Craig" → which people file to patch
    slug_map = {}
    if PEOPLE_DIR.exists():
        for f in PEOPLE_DIR.glob("*.md"):
            slug_map[f.stem] = f

    patched_meetings = 0
    patched_people   = 0

    # ── Apply meeting corrections ──────────────────────────────────────────────
    for filename, mcorr in meeting_corrections.items():
        meeting_path = MEETINGS_DIR / filename
        if not meeting_path.exists():
            print(f"  Meeting not found: {filename}")
            continue

        # Per-meeting people corrections (e.g. Craig → Greg O'Dwyer in this meeting only)
        per_meeting_people = mcorr.get("people_corrections", {})

        if patch_meeting_file(meeting_path, mcorr, per_meeting_people):
            print(f"  Patched meeting: {filename}")
            patched_meetings += 1

    # ── Apply people corrections ───────────────────────────────────────────────
    for raw_name, person_data in people_corrections.items():
        # Find the people file for this person
        # Try slug derived from raw name first, then resolved name
        slug = re.sub(r"[^a-z0-9-]", "", raw_name.lower().replace(" ", "-").replace("'", ""))
        resolved_name = person_data.get("name", raw_name)
        resolved_slug = re.sub(r"[^a-z0-9-]", "", resolved_name.lower().replace(" ", "-").replace("'", ""))

        path = slug_map.get(resolved_slug) or slug_map.get(slug)
        if path and patch_people_file(path, person_data):
            print(f"  Patched person file: {path.name}")
            patched_people += 1

    print(f"\nDone — {patched_meetings} meetings, {patched_people} people files patched.")


if __name__ == "__main__":
    apply()
