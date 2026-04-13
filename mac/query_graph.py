#!/usr/bin/env python3
"""
query_graph.py — Query the knowledge graph for action items, decisions, and meeting history.

Usage:
  python3 ~/query_graph.py open                          # all open action items
  python3 ~/query_graph.py open --project DCC            # open items for DCC
  python3 ~/query_graph.py open --person "Pat Nestor"    # open items assigned to Pat
  python3 ~/query_graph.py decisions --project DCC       # decisions for DCC
  python3 ~/query_graph.py history "Brendan Ryan"        # meeting history with a person
  python3 ~/query_graph.py stats                         # graph stats overview
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

GRAPH_DB = Path.home() / "graph.db"
CONTACTS_DB = Path.home() / "contacts.db"


def get_conn(db_path):
    if not db_path.exists():
        print(f"Error: {db_path} not found. Run build_graph.py first.", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def slugify(name):
    return re.sub(r"[\s_-]+", "-", name.lower()).strip("-")


def meeting_category(filename):
    """Extract category from meeting filename like 2026-04-11_1358_DCC_topic.md"""
    parts = filename.split("_", 3)
    if len(parts) >= 3:
        return parts[2]
    return ""


def fuzzy_owner_match(owner, search_name):
    """Check if owner matches search name (case-insensitive, partial)."""
    if not owner:
        return False
    owner_l = owner.lower().strip("[]")
    search_l = search_name.lower()
    return search_l in owner_l or owner_l in search_l


def cmd_open(args):
    conn = get_conn(GRAPH_DB)

    query = "SELECT id, meeting_filename, text, owner FROM action_items WHERE status = 'open'"
    params = []

    if args.person:
        # Fuzzy match on owner
        query += " AND (LOWER(REPLACE(owner, '[', '')) LIKE ? OR LOWER(REPLACE(owner, '[', '')) LIKE ?)"
        search = args.person.lower()
        params.extend([f"%{search}%", f"%{slugify(args.person)}%"])

    rows = conn.execute(query, params).fetchall()

    # Filter by project if needed (derived from meeting filename)
    if args.project:
        proj = args.project.upper()
        rows = [r for r in rows if meeting_category(r["meeting_filename"]).upper() == proj]

    if not rows:
        print("No open action items found.")
        return

    # Group by meeting, sorted by date (newest first)
    by_meeting = {}
    for r in rows:
        by_meeting.setdefault(r["meeting_filename"], []).append(r)

    sorted_meetings = sorted(by_meeting.keys(), reverse=True)

    total = len(rows)
    proj_label = f" [{args.project.upper()}]" if args.project else ""
    person_label = f" for {args.person}" if args.person else ""
    print(f"## Open Action Items{proj_label}{person_label} ({total} total)\n")

    for meeting in sorted_meetings:
        items = by_meeting[meeting]
        date = meeting.split("_")[0] if "_" in meeting else ""
        cat = meeting_category(meeting)
        print(f"### {date} | {cat}")
        for item in items:
            owner = item["owner"] or "unassigned"
            owner = owner.strip("[]")
            print(f"  - [ ] {owner}: {item['text']}")
        print()

    conn.close()


def cmd_decisions(args):
    conn = get_conn(GRAPH_DB)

    query = "SELECT meeting_filename, text FROM decisions"
    params = []

    rows = conn.execute(query, params).fetchall()

    if args.project:
        proj = args.project.upper()
        rows = [r for r in rows if meeting_category(r["meeting_filename"]).upper() == proj]

    if not rows:
        print("No decisions found.")
        return

    # Group by meeting
    by_meeting = {}
    for r in rows:
        by_meeting.setdefault(r["meeting_filename"], []).append(r)

    sorted_meetings = sorted(by_meeting.keys(), reverse=True)

    proj_label = f" [{args.project.upper()}]" if args.project else ""
    print(f"## Decisions{proj_label} ({len(rows)} total)\n")

    for meeting in sorted_meetings:
        items = by_meeting[meeting]
        date = meeting.split("_")[0] if "_" in meeting else ""
        cat = meeting_category(meeting)
        print(f"### {date} | {cat}")
        for item in items:
            print(f"  - {item['text']}")
        print()

    conn.close()


def cmd_history(args):
    if not args.name:
        print("Usage: query_graph.py history \"Person Name\"", file=sys.stderr)
        sys.exit(1)

    conn = get_conn(GRAPH_DB)
    name_slug = slugify(args.name)

    # Find meetings via graph edges (SPOKE_IN or MENTIONED_IN)
    rows = conn.execute("""
        SELECT DISTINCT to_id as meeting_filename, edge_type
        FROM graph_edges
        WHERE from_type = 'person' AND from_id = ?
          AND edge_type IN ('SPOKE_IN', 'MENTIONED_IN')
        ORDER BY to_id DESC
    """, (name_slug,)).fetchall()

    if not rows:
        # Try partial match
        rows = conn.execute("""
            SELECT DISTINCT to_id as meeting_filename, edge_type
            FROM graph_edges
            WHERE from_type = 'person' AND from_id LIKE ?
              AND edge_type IN ('SPOKE_IN', 'MENTIONED_IN')
            ORDER BY to_id DESC
        """, (f"%{name_slug}%",)).fetchall()

    if not rows:
        print(f"No meeting history found for '{args.name}'")
        return

    limit = args.limit or 10
    rows = rows[:limit]

    print(f"## Meeting History: {args.name} (showing {len(rows)})\n")
    for r in rows:
        fn = r["meeting_filename"]
        date = fn.split("_")[0] if "_" in fn else ""
        cat = meeting_category(fn)
        role = "attendee" if r["edge_type"] == "SPOKE_IN" else "mentioned"
        print(f"  {date} | {cat} | {fn.replace('.md','')} ({role})")

    conn.close()


def cmd_stats(args):
    conn = get_conn(GRAPH_DB)

    meetings = conn.execute("SELECT COUNT(DISTINCT meeting_filename) FROM action_items").fetchone()[0]
    actions = conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0]
    open_actions = conn.execute("SELECT COUNT(*) FROM action_items WHERE status = 'open'").fetchone()[0]
    decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]

    edge_types = conn.execute(
        "SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY edge_type ORDER BY COUNT(*) DESC"
    ).fetchall()

    # Top projects by action items
    all_actions = conn.execute("SELECT meeting_filename FROM action_items WHERE status = 'open'").fetchall()
    by_proj = {}
    for r in all_actions:
        cat = meeting_category(r["meeting_filename"])
        by_proj[cat] = by_proj.get(cat, 0) + 1

    print("## Graph Stats\n")
    print(f"  Meetings with insights: {meetings}")
    print(f"  Action items: {actions} ({open_actions} open)")
    print(f"  Decisions: {decisions}")
    print(f"  Graph edges: {edges}")
    print()
    print("  Edge types:")
    for r in edge_types:
        print(f"    {r[0]}: {r[1]}")
    print()
    print("  Open actions by project:")
    for proj, count in sorted(by_proj.items(), key=lambda x: -x[1]):
        print(f"    {proj}: {count}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Query the knowledge graph")
    sub = parser.add_subparsers(dest="command")

    p_open = sub.add_parser("open", help="List open action items")
    p_open.add_argument("--project", "-p", help="Filter by project/category (e.g. DCC, NTA)")
    p_open.add_argument("--person", help="Filter by assigned person")

    p_dec = sub.add_parser("decisions", help="List decisions")
    p_dec.add_argument("--project", "-p", help="Filter by project/category")

    p_hist = sub.add_parser("history", help="Meeting history with a person")
    p_hist.add_argument("name", nargs="?", help="Person name")
    p_hist.add_argument("--limit", "-n", type=int, default=10)

    p_stats = sub.add_parser("stats", help="Graph stats overview")

    args = parser.parse_args()

    if args.command == "open":
        cmd_open(args)
    elif args.command == "decisions":
        cmd_decisions(args)
    elif args.command == "history":
        cmd_history(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
