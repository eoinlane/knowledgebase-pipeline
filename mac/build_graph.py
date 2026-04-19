#!/usr/bin/env python3
"""
build_graph.py — Build graph.db from KB meeting frontmatter + insights JSONs.

Reads:
  ~/knowledge_base/meetings/*.md   (frontmatter: source_file, category, date, attendees, mentioned)
  /tmp/kb_insights/*.json          (action_items, decisions, follow_ups, open_questions)
  ~/contacts.db                    (meetings table for filename→id mapping)

Outputs:
  ~/graph.db  (decisions, action_items, graph_edges, concepts tables)

Run after build_contacts_db.py:
  python3 ~/knowledgebase-pipeline/mac/build_graph.py
"""

import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

KB_DIR = Path.home() / "knowledge_base" / "meetings"
DOCS_DIR = Path.home() / "knowledge_base" / "documents"
INSIGHTS_DIR = "/tmp/kb_insights"
CONTACTS_DB = Path.home() / "contacts.db"
GRAPH_DB = Path.home() / "graph.db"
CLOSURES_FILE = Path.home() / ".graph_closures.json"
STALE_WEEKS = 8


def rsync_insights():
    """Pull insights JSONs from Ubuntu to /tmp/kb_insights/."""
    os.makedirs(INSIGHTS_DIR, exist_ok=True)
    try:
        result = subprocess.run(
            ["rsync", "-az", "--timeout=10",
             "eoin@nvidiaubuntubox:~/audio-inbox/Insights/", INSIGHTS_DIR + "/"],
            timeout=30, capture_output=True
        )
        if result.returncode != 0:
            print(f"  WARNING: rsync failed (exit {result.returncode}) — using cached insights")
    except subprocess.TimeoutExpired:
        print("  WARNING: rsync timed out — Ubuntu unreachable, using cached insights")
    except FileNotFoundError:
        print("  WARNING: rsync not found — using cached insights")

    # Check we have something to work with
    existing = [f for f in os.listdir(INSIGHTS_DIR) if f.endswith(".json")] if os.path.isdir(INSIGHTS_DIR) else []
    if not existing:
        print("  ERROR: No insights files available — graph will have no action items or decisions")


def parse_frontmatter_and_body(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        return yaml.safe_load(parts[1]) or {}, parts[2]
    except yaml.YAMLError:
        return {}, content


def parse_people_field(field):
    """Handle both ["Name1", "Name2"] and ["Name1, Name2"] YAML formats."""
    if not field:
        return []
    names = []
    for item in field:
        for name in re.split(r"[;,]", item):
            name = name.strip()
            if name:
                names.append(name)
    return names


def slugify(name):
    return re.sub(r"[\s_-]+", "-", name.lower()).strip("-")


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decisions (
            id               INTEGER PRIMARY KEY,
            meeting_filename TEXT,
            text             TEXT,
            project          TEXT,
            status           TEXT DEFAULT 'open',
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS action_items (
            id               INTEGER PRIMARY KEY,
            meeting_filename TEXT,
            text             TEXT,
            owner            TEXT,
            project          TEXT,
            status           TEXT DEFAULT 'open',
            due_date         TEXT,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS graph_edges (
            id          INTEGER PRIMARY KEY,
            from_type   TEXT,
            from_id     TEXT,
            edge_type   TEXT,
            to_type     TEXT,
            to_id       TEXT,
            confidence  REAL DEFAULT 1.0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS concepts (
            id            INTEGER PRIMARY KEY,
            label         TEXT UNIQUE,
            category      TEXT,
            first_seen    TEXT,
            mention_count INTEGER DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_edges_from ON graph_edges(from_type, from_id);
        CREATE INDEX IF NOT EXISTS idx_edges_to   ON graph_edges(to_type, to_id);
        CREATE INDEX IF NOT EXISTS idx_edges_type ON graph_edges(edge_type);
        CREATE INDEX IF NOT EXISTS idx_ai_status  ON action_items(status);
        CREATE INDEX IF NOT EXISTS idx_ai_owner   ON action_items(owner);
        CREATE INDEX IF NOT EXISTS idx_ai_meeting ON action_items(meeting_filename);
        CREATE INDEX IF NOT EXISTS idx_ai_project ON action_items(project);
        CREATE INDEX IF NOT EXISTS idx_dec_meeting ON decisions(meeting_filename);
        CREATE INDEX IF NOT EXISTS idx_dec_project ON decisions(project);

        CREATE TABLE IF NOT EXISTS syntheses (
            id          INTEGER PRIMARY KEY,
            entity_type TEXT,     -- 'person' or 'project'
            entity_id   TEXT,     -- slug or category name
            text        TEXT,
            model       TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_synth_entity ON syntheses(entity_type, entity_id);
    """)


def add_edge(conn, from_type, from_id, edge_type, to_type, to_id, confidence=1.0):
    conn.execute(
        """INSERT INTO graph_edges (from_type, from_id, edge_type, to_type, to_id, confidence)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (from_type, from_id, edge_type, to_type, to_id, confidence)
    )


def load_known_people():
    """Load multi-word people names from contacts.db for transcript scanning."""
    if not CONTACTS_DB.exists():
        return []
    conn = sqlite3.connect(CONTACTS_DB)
    rows = conn.execute("""
        SELECT DISTINCT name FROM people
        WHERE name LIKE '% %'
          AND name NOT LIKE 'SPEAKER%'
          AND name NOT LIKE '%@%'
          AND LENGTH(name) > 5
    """).fetchall()
    # Also grab resolved_name where it differs
    resolved = conn.execute("""
        SELECT DISTINCT resolved_name FROM people
        WHERE resolved_name IS NOT NULL
          AND resolved_name LIKE '% %'
          AND resolved_name NOT LIKE 'SPEAKER%'
          AND LENGTH(resolved_name) > 5
    """).fetchall()
    conn.close()
    names = set(r[0] for r in rows) | set(r[0] for r in resolved)
    # Filter out very short surnames that would cause false positives
    return sorted(names, key=len, reverse=True)  # longest first to avoid partial matches


def scan_transcript_for_names(transcript_body, known_names):
    """Find known people mentioned in transcript text. Returns set of matched names."""
    found = set()
    body_lower = transcript_body.lower()
    for name in known_names:
        if name.lower() in body_lower:
            found.add(name)
    return found


def _ensure_pipeline_path():
    pipeline_dir = os.environ.get("PIPELINE_DIR", os.path.expanduser("~/knowledgebase-pipeline"))
    if os.path.isdir(pipeline_dir) and pipeline_dir not in sys.path:
        sys.path.insert(0, pipeline_dir)

_ensure_pipeline_path()
from shared.entity_resolver import build_resolver, resolve_slug as resolve_person_slug
from shared.project_tagger import build_owner_project_tagger


def build_graph():
    rsync_insights()

    # Load known people roster for transcript scanning
    known_people = load_known_people()
    print(f"  Loaded {len(known_people)} known people for transcript scanning")

    # Build entity resolver
    resolver = build_resolver()
    print(f"  Built resolver with {len(resolver)} mappings")

    # Build UUID → insights JSON mapping
    insights_map = {}
    for fname in os.listdir(INSIGHTS_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(INSIGHTS_DIR, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
            if data.get("skipped"):
                continue
            # Key by filename without .json and without .txt
            key = fname.replace(".json", "").replace(".txt", "")
            insights_map[key] = data
        except (json.JSONDecodeError, OSError):
            continue

    print(f"  Loaded {len(insights_map)} insights files")

    # Scan KB meetings
    md_files = sorted(KB_DIR.glob("*.md"))
    print(f"  Found {len(md_files)} meeting files")

    # Build into temp DB, then atomic swap (so queries never see empty tables)
    tmp_db = Path(str(GRAPH_DB) + ".tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)

    # Carry forward syntheses from existing DB (expensive to regenerate)
    if GRAPH_DB.exists():
        try:
            old_conn = sqlite3.connect(GRAPH_DB)
            old_conn.row_factory = sqlite3.Row
            for row in old_conn.execute("SELECT entity_type, entity_id, text, model, created_at FROM syntheses"):
                conn.execute("INSERT INTO syntheses (entity_type, entity_id, text, model, created_at) VALUES (?, ?, ?, ?, ?)",
                             (row["entity_type"], row["entity_id"], row["text"], row["model"], row["created_at"]))
            old_conn.close()
        except Exception:
            pass  # No syntheses to carry forward

    # Build owner→project tagger
    project_tagger = build_owner_project_tagger()
    print(f"  Built project tagger with {len(project_tagger)} owner mappings")

    stats = {"meetings": 0, "actions": 0, "decisions": 0, "edges": 0, "matched": 0,
             "insights_people": 0, "transcript_people": 0, "project_retagged": 0}

    for md_path in md_files:
        fm, body = parse_frontmatter_and_body(md_path)
        if not fm:
            continue

        meeting_filename = md_path.name
        source_file = fm.get("source_file", "")
        category = fm.get("category", "")
        date = str(fm.get("date", ""))
        attendees = parse_people_field(fm.get("attendees", []))
        mentioned = parse_people_field(fm.get("mentioned", []))

        stats["meetings"] += 1

        # Track all people linked to this meeting (slug → edge_type) to deduplicate
        seen_people = {}

        # Add SPOKE_IN edges for attendees
        for person in attendees:
            slug = resolve_person_slug(slugify(person), resolver)
            if slug and slug not in seen_people:
                add_edge(conn, "person", slug, "SPOKE_IN", "meeting", meeting_filename)
                seen_people[slug] = "SPOKE_IN"
                stats["edges"] += 1

        # Add MENTIONED_IN edges for mentioned people (from frontmatter)
        for person in mentioned:
            slug = resolve_person_slug(slugify(person), resolver)
            if slug and slug not in seen_people:
                add_edge(conn, "person", slug, "MENTIONED_IN", "meeting", meeting_filename)
                seen_people[slug] = "MENTIONED_IN"
                stats["edges"] += 1

        # Add PART_OF edge for category
        if category:
            add_edge(conn, "meeting", meeting_filename, "PART_OF", "category", category)
            stats["edges"] += 1

        # Find matching insights JSON (source_file may have .txt suffix)
        lookup_key = source_file.replace(".txt", "")
        insights = insights_map.get(lookup_key) or insights_map.get(source_file)

        if insights:
            stats["matched"] += 1

            # --- Enrichment 1: Extract people from insights JSON ---
            insights_names = set()
            for ai in insights.get("action_items", []):
                if isinstance(ai, dict):
                    owner = ai.get("owner", "")
                    if owner and " " in owner and not owner.startswith("SPEAKER"):
                        insights_names.add(owner.strip("[]"))
            for fu in insights.get("follow_ups", []):
                if isinstance(fu, dict):
                    who = fu.get("who") or ""
                    if who and " " in who and not who.startswith("SPEAKER"):
                        insights_names.add(who.strip("[]"))

            for name in insights_names:
                slug = resolve_person_slug(slugify(name), resolver)
                if slug and slug not in seen_people:
                    add_edge(conn, "person", slug, "MENTIONED_IN", "meeting", meeting_filename, confidence=0.9)
                    seen_people[slug] = "MENTIONED_IN"
                    stats["edges"] += 1
                    stats["insights_people"] += 1

            # Store action items
            for ai in insights.get("action_items", []):
                if isinstance(ai, dict):
                    text = ai.get("action", "")
                    owner = ai.get("owner", "").strip("[]")
                else:
                    text = str(ai)
                    owner = ""

                if not text:
                    continue

                # Determine project: owner's category if known, else meeting category
                project = project_tagger.get(owner.lower(), "") if owner else ""
                if not project:
                    project = category
                if project != category:
                    stats["project_retagged"] += 1

                cur = conn.execute(
                    "INSERT INTO action_items (meeting_filename, text, owner, project) VALUES (?, ?, ?, ?)",
                    (meeting_filename, text, owner or None, project or None)
                )
                ai_id = cur.lastrowid
                stats["actions"] += 1

                # meeting PRODUCED action_item
                add_edge(conn, "meeting", meeting_filename, "PRODUCED", "action_item", str(ai_id))
                stats["edges"] += 1

                # action_item ASSIGNED_TO person
                if owner:
                    owner_slug = resolve_person_slug(slugify(owner), resolver)
                    if owner_slug:
                        add_edge(conn, "action_item", str(ai_id), "ASSIGNED_TO", "person", owner_slug)
                        stats["edges"] += 1

            # Store decisions (inherit project from meeting category)
            for dec_text in insights.get("decisions", []):
                if not dec_text:
                    continue
                cur = conn.execute(
                    "INSERT INTO decisions (meeting_filename, text, project) VALUES (?, ?, ?)",
                    (meeting_filename, dec_text, category or None)
                )
                dec_id = cur.lastrowid
                stats["decisions"] += 1

                add_edge(conn, "meeting", meeting_filename, "PRODUCED", "decision", str(dec_id))
                stats["edges"] += 1

            # Store follow-ups as edges back to the meeting
            for fu in insights.get("follow_ups", []):
                if isinstance(fu, dict):
                    desc = fu.get("description", "")
                    who = fu.get("who") or ""
                else:
                    desc = str(fu)
                    who = ""
                if desc and who:
                    who_slug = resolve_person_slug(slugify(who), resolver)
                    if who_slug:
                        add_edge(conn, "person", who_slug, "FOLLOW_UP", "meeting", meeting_filename, confidence=0.8)
                        stats["edges"] += 1

        # --- Enrichment 2: Scan transcript body for known people names ---
        if known_people and body:
            found_names = scan_transcript_for_names(body, known_people)
            for name in found_names:
                slug = resolve_person_slug(slugify(name), resolver)
                if slug and slug not in seen_people and slug != "eoin-lane":
                    add_edge(conn, "person", slug, "MENTIONED_IN", "meeting", meeting_filename, confidence=0.8)
                    seen_people[slug] = "MENTIONED_IN"
                    stats["edges"] += 1
                    stats["transcript_people"] += 1

        # --- Enrichment 3: Extract tags from key_topics ---
        if insights:
            for topic_text in insights.get("key_topics", []):
                # Normalise: take first phrase (before — or :), lowercase
                # Use only em-dash and colon as separators (not hyphens)
                label = re.split(r"\s*[—:–]\s*", topic_text)[0].strip()
                if len(label) < 10 or len(label) > 80:
                    continue
                label = label.lower()
                # Upsert into concepts table
                existing = conn.execute(
                    "SELECT id, mention_count FROM concepts WHERE label = ?", (label,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE concepts SET mention_count = mention_count + 1 WHERE id = ?",
                        (existing[0],)
                    )
                    concept_id = existing[0]
                else:
                    cur = conn.execute(
                        "INSERT INTO concepts (label, category, first_seen) VALUES (?, ?, ?)",
                        (label, category, meeting_filename)
                    )
                    concept_id = cur.lastrowid
                add_edge(conn, "meeting", meeting_filename, "DISCUSSED", "concept", str(concept_id))
                stats["edges"] += 1

    # --- Index documents/ into the graph ---
    docs_count = 0
    if DOCS_DIR.exists():
        for doc_path in sorted(DOCS_DIR.glob("*.md")):
            fm, body = parse_frontmatter_and_body(doc_path)
            if not fm:
                continue

            doc_filename = doc_path.name
            category = fm.get("category", "")
            people = fm.get("people", [])
            if isinstance(people, str):
                people = [people]

            # PART_OF edge for category
            if category:
                add_edge(conn, "document", doc_filename, "PART_OF", "category", category)
                stats["edges"] += 1

            # People edges from frontmatter
            for person in people:
                slug = resolve_person_slug(slugify(person), resolver)
                if slug and slug != "eoin-lane":
                    add_edge(conn, "person", slug, "REFERENCED_IN", "document", doc_filename)
                    stats["edges"] += 1

            # Scan body for known people
            if known_people and body:
                found = scan_transcript_for_names(body, known_people)
                seen_doc_people = set()
                for name in found:
                    slug = resolve_person_slug(slugify(name), resolver)
                    if slug and slug != "eoin-lane" and slug not in seen_doc_people:
                        add_edge(conn, "person", slug, "REFERENCED_IN", "document", doc_filename, confidence=0.8)
                        seen_doc_people.add(slug)
                        stats["edges"] += 1

            docs_count += 1

    # --- Auto-age: mark items older than STALE_WEEKS as 'stale' ---
    cutoff = (datetime.date.today() - datetime.timedelta(weeks=STALE_WEEKS)).isoformat()
    stale_count = conn.execute(
        "UPDATE action_items SET status = 'stale' WHERE status = 'open' AND meeting_filename < ?",
        (cutoff,)
    ).rowcount

    # --- Apply manual closures from ~/.graph_closures.json ---
    closures_applied = 0
    if CLOSURES_FILE.exists():
        try:
            with open(CLOSURES_FILE) as f:
                closures = json.load(f)
            for key, status in closures.items():
                # Key format: "meeting_filename::action text prefix"
                parts = key.split("::", 1)
                if len(parts) == 2:
                    meeting_fn, text_prefix = parts
                    n = conn.execute(
                        "UPDATE action_items SET status = ? WHERE meeting_filename = ? AND text LIKE ?",
                        (status, meeting_fn, text_prefix + "%")
                    ).rowcount
                    closures_applied += n
        except (json.JSONDecodeError, OSError):
            pass

    conn.commit()
    conn.close()

    # Atomic swap: replace old DB with new one
    os.replace(tmp_db, GRAPH_DB)

    open_count = sqlite3.connect(GRAPH_DB).execute(
        "SELECT COUNT(*) FROM action_items WHERE status = 'open'"
    ).fetchone()[0]

    print(f"\nDone — {GRAPH_DB}")
    print(f"  {stats['meetings']} meetings processed")
    print(f"  {stats['matched']} matched to insights ({stats['meetings'] - stats['matched']} without insights)")
    print(f"  {stats['actions']} action items ({open_count} open, {stale_count} auto-staled, {closures_applied} manually closed)")
    print(f"  {stats['decisions']} decisions")
    print(f"  {stats['edges']} graph edges")
    print(f"  {docs_count} documents indexed")
    print(f"  People enrichment: {stats['insights_people']} from insights, {stats['transcript_people']} from transcript scan")
    print(f"  Project tagging: {stats['project_retagged']} action items retagged by owner")


if __name__ == "__main__":
    build_graph()
