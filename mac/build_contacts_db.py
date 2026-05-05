#!/usr/bin/env python3
"""
build_contacts_db.py — Build SQLite contacts database from knowledge base markdown files.
Run from ~/: python3 ~/knowledgebase-pipeline/build_contacts_db.py
Output: ~/contacts.db
"""

import json
import sqlite3
import re
import yaml
from pathlib import Path
from collections import Counter
from entity_resolution import build_suggestions

CORRECTIONS_FILE = Path.home() / "kb_corrections.json"

KB_DIR = Path.home() / "knowledge_base"
DB_PATH = Path.home() / "contacts.db"


def parse_frontmatter(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        fm = yaml.safe_load(parts[1])
        return fm or {}, parts[2]
    except yaml.YAMLError:
        return {}, content


def parse_people_field(people_field):
    """Handle both ["Name1", "Name2"] and ["Name1, Name2, Name3"] YAML formats."""
    if not people_field:
        return []
    skip = {"eoin lane", "eoin", "owen lane", "owen", ""}
    names = []
    for entry in people_field:
        if not isinstance(entry, str):
            continue
        for name in entry.split(","):
            name = name.strip()
            if name.lower() not in skip:
                names.append(name)
    return names


def extract_summary(body):
    match = re.search(r"## Summary\s*\n(.*?)(?=\n##|\Z)", body, re.DOTALL)
    if match:
        return match.group(1).strip()[:600]
    return ""


def build_db():
    meetings_dir = KB_DIR / "meetings"
    people_dir = KB_DIR / "people"

    if not meetings_dir.exists():
        print(f"Error: meetings dir not found: {meetings_dir}")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.executescript("""
        DROP TABLE IF EXISTS attendees;
        DROP TABLE IF EXISTS meetings;
        DROP TABLE IF EXISTS people;

        CREATE TABLE meetings (
            id       INTEGER PRIMARY KEY,
            filename TEXT UNIQUE,
            title    TEXT,
            date     TEXT,
            category TEXT,
            topic    TEXT,
            summary  TEXT,
            tags     TEXT DEFAULT '[]'
        );

        CREATE TABLE people (
            id            INTEGER PRIMARY KEY,
            name          TEXT UNIQUE,
            slug          TEXT,
            primary_org   TEXT,
            meeting_count INTEGER DEFAULT 0,
            last_seen     TEXT,
            has_file      INTEGER DEFAULT 0,
            resolved_name TEXT,
            resolved_slug TEXT,
            title         TEXT,
            org_detail    TEXT
        );

        CREATE TABLE attendees (
            meeting_id  INTEGER REFERENCES meetings(id),
            person_name TEXT,
            PRIMARY KEY (meeting_id, person_name)
        );
    """)

    # ── meetings ──────────────────────────────────────────────────────────────
    person_appearances = {}  # name -> [(date, category)]
    meeting_files = sorted(meetings_dir.glob("*.md"))
    print(f"Parsing {len(meeting_files)} meeting files...")

    for f in meeting_files:
        fm, body = parse_frontmatter(f)
        if not fm:
            continue

        date     = str(fm.get("date", ""))
        category = str(fm.get("category", ""))
        title    = str(fm.get("title", f.stem))
        topic    = str(fm.get("topic", ""))
        summary  = extract_summary(body)

        c.execute(
            "INSERT OR IGNORE INTO meetings (filename, title, date, category, topic, summary) "
            "VALUES (?,?,?,?,?,?)",
            (f.name, title, date, category, topic, summary),
        )
        meeting_id = c.execute(
            "SELECT id FROM meetings WHERE filename=?", (f.name,)
        ).fetchone()[0]

        for name in parse_people_field(fm.get("people", [])):
            c.execute(
                "INSERT OR IGNORE INTO attendees (meeting_id, person_name) VALUES (?,?)",
                (meeting_id, name),
            )
            person_appearances.setdefault(name, []).append((date, category))

    # ── people ────────────────────────────────────────────────────────────────
    print(f"Building records for {len(person_appearances)} people...")

    people_slugs = (
        {f.stem for f in people_dir.glob("*.md")} if people_dir.exists() else set()
    )

    for name, appearances in person_appearances.items():
        dates      = [d for d, _ in appearances if d]
        categories = [cat for _, cat in appearances if cat]
        primary    = Counter(categories).most_common(1)[0][0] if categories else ""
        last_seen  = max(dates) if dates else ""
        slug       = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-").replace("'", ""))
        has_file   = 1 if slug in people_slugs else 0

        c.execute(
            "INSERT OR REPLACE INTO people (name, slug, primary_org, meeting_count, last_seen, has_file) "
            "VALUES (?,?,?,?,?,?)",
            (name, slug, primary, len(appearances), last_seen, has_file),
        )

    conn.commit()

    # ── name resolution ───────────────────────────────────────────────────────
    if people_dir.exists():
        resolve_names(conn, people_dir)

    # ── apply manual corrections file ─────────────────────────────────────────
    apply_manual_corrections(conn)

    # ── entity resolution ──────────────────────────────────────────────────────
    build_suggestions(conn)

    n_meetings  = c.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    n_people    = c.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    n_resolved  = c.execute("SELECT COUNT(*) FROM people WHERE resolved_name IS NOT NULL").fetchone()[0]
    print(f"\nDone — {DB_PATH}")
    print(f"  {n_meetings} meetings indexed")
    print(f"  {n_people} people ({n_resolved} names resolved)")

    conn.close()


def apply_manual_corrections(conn):
    """Apply ~/kb_corrections.json as hard overrides after auto-resolution."""
    if not CORRECTIONS_FILE.exists():
        return
    with open(CORRECTIONS_FILE) as f:
        corrections = json.load(f)

    c = conn.cursor()
    n = 0

    for raw_name, person_data in corrections.get("people", {}).items():
        resolved_name = person_data.get("name")
        title         = person_data.get("title")
        org_detail    = person_data.get("org")

        # Guard: refuse to collapse a multi-word name to a shorter form.
        # Reverse-direction entries like {"Jamie Cudden": {"name": "Jamie"}}
        # silently corrupted resolution by mapping 3 distinct Jamies onto a
        # single first-name slug. Legitimate exception: stripping a
        # parenthetical (e.g. "Stephen Rigney ( ADAPT Research Centre )"
        # → "Stephen Rigney"). We allow that by checking whether the raw
        # name with parens stripped equals the resolved name.
        if resolved_name and isinstance(resolved_name, str):
            raw_tokens = len(raw_name.split())
            new_tokens = len(resolved_name.split())
            if new_tokens < raw_tokens:
                stripped = re.sub(r"\s*\([^)]*\)\s*", " ", raw_name).strip()
                if stripped.lower() != resolved_name.lower():
                    print(f"  SKIP corruption: '{raw_name}' → '{resolved_name}' would shorten")
                    continue

        slug = re.sub(r"[^a-z0-9-]", "", (resolved_name or raw_name).lower()
                      .replace(" ", "-").replace("'", ""))
        c.execute("""
            UPDATE people SET
                resolved_name = COALESCE(?, resolved_name),
                resolved_slug = COALESCE(?, resolved_slug),
                title         = COALESCE(?, title),
                org_detail    = COALESCE(?, org_detail)
            WHERE name = ?
        """, (resolved_name, slug if resolved_name else None,
              title, org_detail, raw_name))
        if c.rowcount:
            n += 1

    for filename, mcorr in corrections.get("meetings", {}).items():
        tags  = json.dumps(mcorr.get("tags", []))
        topic = mcorr.get("topic")
        c.execute("""
            UPDATE meetings SET
                tags  = ?,
                topic = COALESCE(?, topic),
                title = COALESCE(?, title)
            WHERE filename = ?
        """, (tags, topic, topic, filename))

    conn.commit()
    if n:
        print(f"  Applied manual corrections for {n} people")


def parse_people_file_meetings(filepath):
    """Extract meeting filenames listed in a people/*.md file."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    return set(re.findall(r"\[\[meetings/([^\]]+\.md)\]\]", content))


def resolve_names(conn, people_dir):
    """Match first-name-only DB entries to full names using meeting overlap."""
    c = conn.cursor()

    # Build index: first_name (lower) -> [(full_name, slug, {meeting_filenames})]
    candidates = {}
    for pf in people_dir.glob("*.md"):
        fm, _ = parse_frontmatter(pf)
        full_name = fm.get("name", "")
        if not full_name or not isinstance(full_name, str):
            continue
        first = full_name.strip().split()[0].lower()
        meeting_set = parse_people_file_meetings(pf)
        candidates.setdefault(first, []).append((full_name.strip(), pf.stem, meeting_set))

    # Get all DB people that don't already have a resolved name
    rows = c.execute(
        "SELECT id, name FROM people WHERE resolved_name IS NULL"
    ).fetchall()

    resolved = 0
    for pid, name in rows:
        first = name.strip().split()[0].lower()
        matches = candidates.get(first, [])
        if not matches:
            continue

        # Get the meeting filenames this person attended
        attended = set(
            r[0] for r in c.execute(
                "SELECT m.filename FROM attendees a "
                "JOIN meetings m ON m.id = a.meeting_id "
                "WHERE a.person_name = ?", (name,)
            ).fetchall()
        )

        # Guard: never resolve a multi-word DB name to a fewer-tokens
        # people-file name. People files like alex.md / david.md / jamie.md
        # have single-word `name:` frontmatter, which would otherwise collapse
        # all "Alex Surname" / "David Surname" rows onto the wrong slug.
        name_tokens = len(name.split())

        if len(matches) == 1:
            # Only one candidate — resolve if at least 1 meeting overlaps (or no meetings yet)
            full_name, slug, cand_meetings = matches[0]
            if len(full_name.split()) < name_tokens:
                continue  # would collapse multi-word → fewer-token; skip
            overlap = len(attended & cand_meetings)
            if overlap > 0 or not cand_meetings:
                c.execute(
                    "UPDATE people SET resolved_name=?, resolved_slug=? WHERE id=?",
                    (full_name, slug, pid),
                )
                resolved += 1
        else:
            # Multiple candidates — pick highest meeting overlap, require at least 1
            best_name, best_slug, best_overlap = None, None, 0
            for full_name, slug, cand_meetings in matches:
                if len(full_name.split()) < name_tokens:
                    continue
                overlap = len(attended & cand_meetings)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_name, best_slug = full_name, slug
            if best_overlap > 0:
                c.execute(
                    "UPDATE people SET resolved_name=?, resolved_slug=? WHERE id=?",
                    (best_name, best_slug, pid),
                )
                resolved += 1

    conn.commit()
    print(f"  Resolved {resolved} of {len(rows)} ambiguous names")


if __name__ == "__main__":
    build_db()
