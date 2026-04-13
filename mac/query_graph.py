#!/usr/bin/env python3
"""
query_graph.py — Query the knowledge graph for action items, decisions, and meeting history.

Usage:
  python3 ~/query_graph.py prep "Pat Nestor"             # pre-meeting briefing
  python3 ~/query_graph.py prep "Pat Nestor" -p DCC      # briefing scoped to project
  python3 ~/query_graph.py open                          # all open action items
  python3 ~/query_graph.py open --project DCC            # open items for DCC
  python3 ~/query_graph.py open --person "Pat Nestor"    # open items assigned to Pat
  python3 ~/query_graph.py done 42                       # mark action item #42 as done
  python3 ~/query_graph.py done "send Pat the spec"      # mark by text match
  python3 ~/query_graph.py done --stale 6                # close items older than 6 weeks
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


def meeting_date(filename):
    """Extract date string from meeting filename."""
    return filename.split("_")[0] if "_" in filename else ""


def meeting_title(filename):
    """Extract human-readable title from meeting filename."""
    parts = filename.replace(".md", "").split("_", 3)
    if len(parts) >= 4:
        return parts[3].replace("-", " ").title()
    return filename


def cmd_prep(args):
    if not args.name:
        print("Usage: query_graph.py prep \"Person Name\" [--project X]", file=sys.stderr)
        sys.exit(1)

    conn = get_conn(GRAPH_DB)
    name_slug = slugify(args.name)
    today = __import__("datetime").date.today().isoformat()

    # --- Find meetings with this person ---
    meetings = conn.execute("""
        SELECT DISTINCT to_id as meeting_filename, edge_type
        FROM graph_edges
        WHERE from_type = 'person' AND from_id = ?
          AND edge_type IN ('SPOKE_IN', 'MENTIONED_IN')
        ORDER BY to_id DESC
    """, (name_slug,)).fetchall()

    if not meetings:
        meetings = conn.execute("""
            SELECT DISTINCT to_id as meeting_filename, edge_type
            FROM graph_edges
            WHERE from_type = 'person' AND from_id LIKE ?
              AND edge_type IN ('SPOKE_IN', 'MENTIONED_IN')
            ORDER BY to_id DESC
        """, (f"%{name_slug}%",)).fetchall()

    # Filter by project if specified
    if args.project:
        proj = args.project.upper()
        meetings = [m for m in meetings if meeting_category(m["meeting_filename"]).upper() == proj]

    # Determine the project from most recent meeting if not specified
    project = args.project.upper() if args.project else ""
    if not project and meetings:
        project = meeting_category(meetings[0]["meeting_filename"])

    meeting_filenames = [m["meeting_filename"] for m in meetings]

    # --- Header ---
    print(f"# Pre-Meeting Briefing: {args.name} | {project}")
    print(f"Generated: {today}")
    print()

    # --- Last meetings ---
    recent = meetings[:5]
    if recent:
        print("## Recent Meetings")
        print()
        for m in recent:
            fn = m["meeting_filename"]
            date = meeting_date(fn)
            cat = meeting_category(fn)
            title = meeting_title(fn)
            role = "attendee" if m["edge_type"] == "SPOKE_IN" else "mentioned"
            print(f"  {date} | {cat} | {title} ({role})")
        print()

    # --- Their open action items ---
    their_items = conn.execute("""
        SELECT id, meeting_filename, text, owner FROM action_items
        WHERE status = 'open'
          AND (LOWER(owner) LIKE ? OR LOWER(owner) LIKE ?)
    """, (f"%{args.name.lower()}%", f"%{name_slug}%")).fetchall()

    if project:
        their_items = [r for r in their_items if meeting_category(r["meeting_filename"]).upper() == project.upper()]

    if their_items:
        print(f"## Their Open Action Items ({len(their_items)})")
        print()
        for item in sorted(their_items, key=lambda r: r["meeting_filename"], reverse=True)[:10]:
            date = meeting_date(item["meeting_filename"])
            print(f"  - [ ] {item['text']} (from {date}, #{item['id']})")
        if len(their_items) > 10:
            print(f"  ... and {len(their_items) - 10} more")
        print()

    # --- Your open action items for this project ---
    your_items = conn.execute("""
        SELECT id, meeting_filename, text FROM action_items
        WHERE status = 'open'
          AND LOWER(owner) LIKE '%eoin%'
    """).fetchall()

    if project:
        your_items = [r for r in your_items if meeting_category(r["meeting_filename"]).upper() == project.upper()]

    # Further filter to items from meetings where this person was present
    if meeting_filenames:
        your_relevant = [r for r in your_items if r["meeting_filename"] in meeting_filenames]
    else:
        your_relevant = your_items[:10]

    if your_relevant:
        print(f"## Your Open Action Items ({len(your_relevant)} related)")
        print()
        for item in sorted(your_relevant, key=lambda r: r["meeting_filename"], reverse=True)[:10]:
            date = meeting_date(item["meeting_filename"])
            print(f"  - [ ] {item['text']} (from {date}, #{item['id']})")
        if len(your_relevant) > 10:
            print(f"  ... and {len(your_relevant) - 10} more")
        print()

    # --- Recent decisions for this project ---
    if project:
        decisions = conn.execute("SELECT meeting_filename, text FROM decisions").fetchall()
        decisions = [d for d in decisions if meeting_category(d["meeting_filename"]).upper() == project.upper()]
        decisions = sorted(decisions, key=lambda d: d["meeting_filename"], reverse=True)[:10]

        if decisions:
            print(f"## Recent Decisions [{project}]")
            print()
            for d in decisions:
                date = meeting_date(d["meeting_filename"])
                print(f"  - {d['text']} ({date})")
            print()

    # --- Open questions from recent meetings with this person ---
    # Pull from meetings table via contacts.db if available
    if meeting_filenames:
        recent_meetings = meeting_filenames[:3]
        print("## Things to Check")
        print()
        # Check their action items for overdue signals
        for item in (their_items or [])[:5]:
            date = meeting_date(item["meeting_filename"])
            print(f"  - Has \"{item['text'][:60]}...\" been done? (from {date})")
        if your_relevant:
            for item in your_relevant[:3]:
                date = meeting_date(item["meeting_filename"])
                print(f"  - Have you done: \"{item['text'][:60]}...\"? (from {date})")
        print()

    conn.close()


def cmd_done(args):
    conn = get_conn(GRAPH_DB)

    if args.stale:
        # Close all items older than N weeks
        import datetime
        cutoff = (datetime.date.today() - datetime.timedelta(weeks=args.stale)).isoformat()
        rows = conn.execute("""
            SELECT id, meeting_filename, text, owner FROM action_items
            WHERE status = 'open' AND meeting_filename < ?
        """, (cutoff,)).fetchall()

        if not rows:
            print(f"No open items older than {args.stale} weeks.")
            conn.close()
            return

        print(f"Closing {len(rows)} action items older than {args.stale} weeks (before {cutoff})...")
        conn.execute("""
            UPDATE action_items SET status = 'closed'
            WHERE status = 'open' AND meeting_filename < ?
        """, (cutoff,))
        conn.commit()

        # Show summary by project
        by_proj = {}
        for r in rows:
            cat = meeting_category(r["meeting_filename"])
            by_proj[cat] = by_proj.get(cat, 0) + 1
        for proj, count in sorted(by_proj.items(), key=lambda x: -x[1]):
            print(f"  {proj}: {count} closed")

        remaining = conn.execute("SELECT COUNT(*) FROM action_items WHERE status = 'open'").fetchone()[0]
        print(f"\n{remaining} action items still open.")
        conn.close()
        return

    if not args.target:
        print("Usage: query_graph.py done <id> or done \"search text\" or done --stale <weeks>", file=sys.stderr)
        sys.exit(1)

    target = args.target

    # Try as numeric ID first
    try:
        item_id = int(target)
        row = conn.execute("SELECT id, text, owner, status FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            print(f"No action item with ID #{item_id}")
            conn.close()
            return
        if row["status"] == "closed":
            print(f"#{item_id} is already closed.")
            conn.close()
            return
        conn.execute("UPDATE action_items SET status = 'closed' WHERE id = ?", (item_id,))
        conn.commit()
        owner = row["owner"] or "unassigned"
        print(f"Closed #{item_id}: {owner}: {row['text']}")
        conn.close()
        return
    except ValueError:
        pass

    # Text search
    rows = conn.execute("""
        SELECT id, meeting_filename, text, owner FROM action_items
        WHERE status = 'open' AND LOWER(text) LIKE ?
        ORDER BY meeting_filename DESC
    """, (f"%{target.lower()}%",)).fetchall()

    if not rows:
        print(f"No open action items matching \"{target}\"")
        conn.close()
        return

    if len(rows) == 1:
        r = rows[0]
        conn.execute("UPDATE action_items SET status = 'closed' WHERE id = ?", (r["id"],))
        conn.commit()
        owner = r["owner"] or "unassigned"
        print(f"Closed #{r['id']}: {owner}: {r['text']}")
    else:
        print(f"Found {len(rows)} matching items:\n")
        for r in rows[:15]:
            date = meeting_date(r["meeting_filename"])
            owner = r["owner"] or "unassigned"
            print(f"  #{r['id']}  {date} | {owner}: {r['text'][:80]}")
        print(f"\nRun: query_graph.py done <id> to close a specific one.")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Query the knowledge graph")
    sub = parser.add_subparsers(dest="command")

    p_prep = sub.add_parser("prep", help="Pre-meeting briefing for a person")
    p_prep.add_argument("name", nargs="?", help="Person name")
    p_prep.add_argument("--project", "-p", help="Filter by project/category")

    p_open = sub.add_parser("open", help="List open action items")
    p_open.add_argument("--project", "-p", help="Filter by project/category (e.g. DCC, NTA)")
    p_open.add_argument("--person", help="Filter by assigned person")

    p_done = sub.add_parser("done", help="Mark action items as done")
    p_done.add_argument("target", nargs="?", help="Action item ID or search text")
    p_done.add_argument("--stale", type=int, help="Close all items older than N weeks")

    p_dec = sub.add_parser("decisions", help="List decisions")
    p_dec.add_argument("--project", "-p", help="Filter by project/category")

    p_hist = sub.add_parser("history", help="Meeting history with a person")
    p_hist.add_argument("name", nargs="?", help="Person name")
    p_hist.add_argument("--limit", "-n", type=int, default=10)

    p_stats = sub.add_parser("stats", help="Graph stats overview")

    args = parser.parse_args()

    if args.command == "prep":
        cmd_prep(args)
    elif args.command == "open":
        cmd_open(args)
    elif args.command == "done":
        cmd_done(args)
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
