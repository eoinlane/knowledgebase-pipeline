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
  python3 ~/query_graph.py tags                          # top tags across all projects
  python3 ~/query_graph.py tags --project NTA             # top NTA tags
  python3 ~/query_graph.py tags "digital twin"            # find meetings about digital twins
  python3 ~/query_graph.py synthesise "Pat Nestor"        # progressive summary for a person
  python3 ~/query_graph.py synthesise --project DCC      # progressive summary for a project
  python3 ~/query_graph.py review                        # this week's digest
  python3 ~/query_graph.py review --weeks 2              # last 2 weeks
  python3 ~/query_graph.py decisions --project DCC       # decisions for DCC
  python3 ~/query_graph.py history "Brendan Ryan"        # meeting history with a person
  python3 ~/query_graph.py stats                         # graph stats overview
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

GRAPH_DB = Path.home() / "graph.db"
CONTACTS_DB = Path.home() / "contacts.db"
CLOSURES_FILE = Path.home() / ".graph_closures.json"


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


def _build_last_seen_cache(conn, today=None):
    """Return {owner_lower: weeks_since_their_most_recent_meeting}.
    'Meeting' here = the most recent action_item the owner appears on (proxy
    for last interaction). Used by _priority_score to boost items from people
    you still meet vs people you haven't seen since."""
    import datetime as _ndt
    if today is None:
        today = _ndt.date.today()
    cache = {}
    rows = conn.execute("""
        SELECT LOWER(REPLACE(REPLACE(owner, '[', ''), ']', '')) AS o,
               MAX(meeting_filename) AS last_mtg
        FROM action_items
        WHERE owner IS NOT NULL AND owner != ''
        GROUP BY o
    """).fetchall()
    for r in rows:
        date_s = (r["last_mtg"] or "").split("_")[0]
        try:
            d = _ndt.date.fromisoformat(date_s)
            cache[r["o"]] = (today - d).days / 7
        except ValueError:
            continue
    return cache


def _priority_score(meeting_filename, owner, today, last_seen_cache):
    """0..1.5 priority score. Higher = more relevant. Combines:
    - item age (newer = higher; halves at 10 weeks)
    - relationship freshness (recently met = bonus)
    Items from people you haven't seen in months sink to the bottom even if
    the item itself is old; recent items from active relationships float to
    the top. Stale-status items handled separately by the SQL filter."""
    import datetime as _ndt
    date_s = (meeting_filename or "").split("_")[0]
    try:
        item_date = _ndt.date.fromisoformat(date_s)
    except ValueError:
        return 0.0
    age_weeks = max((today - item_date).days / 7, 0)
    age_score = 1.0 / (1.0 + age_weeks * 0.1)  # halves at 10 weeks, 1/4 at 30 weeks

    owner_l = (owner or "").lower().strip("[]")
    last_seen = last_seen_cache.get(owner_l, 999)
    if last_seen <= 4:
        rel_bonus = 0.5
    elif last_seen <= 12:
        rel_bonus = 0.25
    else:
        rel_bonus = 0.0
    return age_score + rel_bonus


def cmd_open(args):
    conn = get_conn(GRAPH_DB)

    query = "SELECT id, meeting_filename, text, owner, project FROM action_items WHERE status = 'open'"
    params = []

    if args.person:
        # Fuzzy match on owner
        query += " AND (LOWER(REPLACE(owner, '[', '')) LIKE ? OR LOWER(REPLACE(owner, '[', '')) LIKE ?)"
        search = args.person.lower()
        params.extend([f"%{search}%", f"%{slugify(args.person)}%"])

    rows = conn.execute(query, params).fetchall()

    # Filter by project — use project column if available, fall back to meeting filename
    if args.project:
        proj = args.project.upper()
        rows = [r for r in rows if (r["project"] or meeting_category(r["meeting_filename"])).upper() == proj]

    if not rows:
        print("No open action items found.")
        return

    # Sort: by-priority (default) blends item age + relationship recency so
    # ancient commitments from people you've not seen sink. --by-date keeps
    # the legacy strict-date-desc grouping for power users who want it.
    by_date = getattr(args, "by_date", False)
    if by_date:
        by_meeting = {}
        for r in rows:
            by_meeting.setdefault(r["meeting_filename"], []).append(r)
        sorted_meetings = sorted(by_meeting.keys(), reverse=True)
        total = len(rows)
        proj_label = f" [{args.project.upper()}]" if args.project else ""
        person_label = f" for {args.person}" if args.person else ""
        print(f"## Open Action Items{proj_label}{person_label} ({total} total, by date)\n")
        for meeting in sorted_meetings:
            items = by_meeting[meeting]
            date = meeting.split("_")[0] if "_" in meeting else ""
            cat = meeting_category(meeting)
            print(f"### {date} | {cat}")
            for item in items:
                owner = (item["owner"] or "unassigned").strip("[]")
                print(f"  - [ ] {owner}: {item['text']}")
            print()
        conn.close()
        return

    # Priority-ordered output
    today = _dt.date.today()
    last_seen_cache = _build_last_seen_cache(conn, today)
    scored = [(_priority_score(r["meeting_filename"], r["owner"], today, last_seen_cache), r)
              for r in rows]
    scored.sort(key=lambda x: -x[0])
    total = len(rows)
    proj_label = f" [{args.project.upper()}]" if args.project else ""
    person_label = f" for {args.person}" if args.person else ""
    print(f"## Open Action Items{proj_label}{person_label} ({total} total, by priority)\n")
    print(f"_Priority = item age × relationship recency. Use `--by-date` for strict date order._\n")

    # Bucket by score band for scanability
    fresh = [s for s in scored if s[0] >= 0.8]
    warm  = [s for s in scored if 0.4 <= s[0] < 0.8]
    cool  = [s for s in scored if 0.2 <= s[0] < 0.4]
    cold  = [s for s in scored if s[0] < 0.2]
    for label, bucket in [("Fresh", fresh), ("Warm", warm), ("Cool", cool), ("Cold", cold)]:
        if not bucket:
            continue
        print(f"### {label} ({len(bucket)})")
        for score, r in bucket:
            owner = (r["owner"] or "unassigned").strip("[]")
            date = r["meeting_filename"].split("_")[0] if "_" in r["meeting_filename"] else ""
            cat = meeting_category(r["meeting_filename"])
            print(f"  - [ ] [{date} {cat} · {score:.2f}] {owner}: {r['text']}")
        print()

    conn.close()


def cmd_decisions(args):
    conn = get_conn(GRAPH_DB)

    query = "SELECT meeting_filename, text, project FROM decisions"
    params = []

    rows = conn.execute(query, params).fetchall()

    if args.project:
        proj = args.project.upper()
        rows = [r for r in rows if (r["project"] or meeting_category(r["meeting_filename"])).upper() == proj]

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


def cmd_context(args):
    """Compact context block for a person — designed to be loaded before
    drafting outbound email/chat to them. Shows: last meeting + summary,
    their open commitments to Eoin, Eoin's open commitments to them, and
    recent cadence. Output is short prose markdown so it pastes cleanly
    into a Gmail-MCP system prompt or into Eoin's own scratchpad."""
    if not args.name:
        print("Usage: query_graph.py context \"Person Name\"", file=sys.stderr)
        sys.exit(1)

    name = args.name
    name_l = name.lower()
    conn = get_conn(GRAPH_DB)

    # Most recent meeting filename involving this person (as owner OR via the graph_edges from-id)
    last_mtg = conn.execute("""
        SELECT meeting_filename, MAX(meeting_filename) AS m
        FROM action_items
        WHERE LOWER(REPLACE(REPLACE(owner, '[', ''), ']', '')) LIKE ?
    """, (f"%{name_l}%",)).fetchone()
    last_meeting_fn = last_mtg["m"] if last_mtg else None

    # Cadence: count of distinct meetings in the last 12 weeks
    today = _dt.date.today()
    cutoff_12w = (today - _dt.timedelta(weeks=12)).isoformat()
    cadence_row = conn.execute("""
        SELECT COUNT(DISTINCT meeting_filename) AS n
        FROM action_items
        WHERE LOWER(REPLACE(REPLACE(owner, '[', ''), ']', '')) LIKE ?
          AND meeting_filename >= ?
    """, (f"%{name_l}%", cutoff_12w)).fetchone()
    cadence_count = cadence_row["n"] if cadence_row else 0

    # Their open items (they owe Eoin)
    they_owe = conn.execute("""
        SELECT meeting_filename, text FROM action_items
        WHERE status = 'open'
          AND LOWER(REPLACE(REPLACE(owner, '[', ''), ']', '')) LIKE ?
        ORDER BY meeting_filename DESC LIMIT 5
    """, (f"%{name_l}%",)).fetchall()

    # Eoin's open items where this person was a meeting participant — proxy
    # via meeting filenames where the person appears AND Eoin is the owner.
    # First find all meeting filenames they touched (any role).
    their_meetings = {
        r["meeting_filename"] for r in conn.execute("""
            SELECT DISTINCT meeting_filename FROM action_items
            WHERE LOWER(REPLACE(REPLACE(owner, '[', ''), ']', '')) LIKE ?
        """, (f"%{name_l}%",)).fetchall()
    }
    eoin_to_them = []
    if their_meetings:
        placeholders = ",".join("?" * len(their_meetings))
        eoin_to_them = conn.execute(f"""
            SELECT meeting_filename, text FROM action_items
            WHERE status = 'open'
              AND LOWER(owner) LIKE '%eoin%'
              AND meeting_filename IN ({placeholders})
            ORDER BY meeting_filename DESC LIMIT 5
        """, tuple(their_meetings)).fetchall()

    # Recent decisions involving this person's meetings
    recent_decisions = []
    if their_meetings:
        placeholders = ",".join("?" * len(their_meetings))
        recent_decisions = conn.execute(f"""
            SELECT meeting_filename, text FROM decisions
            WHERE meeting_filename IN ({placeholders})
            ORDER BY meeting_filename DESC LIMIT 3
        """, tuple(their_meetings)).fetchall()

    # Render
    print(f"# Context for {name}")
    if last_meeting_fn:
        last_date = last_meeting_fn.split("_")[0]
        try:
            d = _dt.date.fromisoformat(last_date)
            days_ago = (today - d).days
            ago = f"{days_ago}d ago" if days_ago < 90 else f"{days_ago // 7}w ago"
        except ValueError:
            ago = "?"
        cat = meeting_category(last_meeting_fn)
        title = meeting_title(last_meeting_fn)
        print(f"\n_Last touched: {last_date} ({ago}) — {cat} · {title}_")
        print(f"_Recent cadence: {cadence_count} meeting(s) in last 12 weeks_")
    else:
        print(f"\n_No recent meetings found involving {name}._")
        conn.close()
        return

    if they_owe:
        print(f"\n## {name} owes you ({len(they_owe)})")
        for r in they_owe:
            text = r["text"]
            if len(text) > 180:
                text = text[:180].rstrip() + "…"
            print(f"- [{meeting_date(r['meeting_filename'])}] {text}")

    if eoin_to_them:
        print(f"\n## You owe {name} ({len(eoin_to_them)})")
        for r in eoin_to_them:
            text = r["text"]
            if len(text) > 180:
                text = text[:180].rstrip() + "…"
            print(f"- [{meeting_date(r['meeting_filename'])}] {text}")

    if recent_decisions:
        print(f"\n## Recent decisions from your meetings together")
        for r in recent_decisions[:3]:
            text = r["text"]
            if len(text) > 180:
                text = text[:180].rstrip() + "…"
            print(f"- [{meeting_date(r['meeting_filename'])}] {text}")

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
    all_actions = conn.execute("SELECT meeting_filename, project FROM action_items WHERE status = 'open'").fetchall()
    by_proj = {}
    for r in all_actions:
        cat = r["project"] or meeting_category(r["meeting_filename"])
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


def save_closure(meeting_filename, text, status="closed"):
    """Persist a closure to ~/.graph_closures.json so it survives rebuilds.
    Stores {"status": ..., "closed_at": ISO-8601 timestamp}. build_graph.py
    reads both this dict form and the legacy plain-string form for
    backwards compatibility with older closures."""
    import datetime as _ndt
    closures = {}
    if CLOSURES_FILE.exists():
        try:
            with open(CLOSURES_FILE) as f:
                closures = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    # Key by meeting_filename::text_prefix (first 80 chars for robustness)
    key = f"{meeting_filename}::{text[:80]}"
    closures[key] = {
        "status": status,
        "closed_at": _ndt.datetime.now().isoformat(timespec="seconds"),
    }
    with open(CLOSURES_FILE, "w") as f:
        json.dump(closures, f, indent=2)


import datetime as _dt


def meeting_date(filename):
    """Extract date string from meeting filename."""
    return filename.split("_")[0] if "_" in filename else ""


def meeting_title(filename):
    """Extract human-readable title from meeting filename."""
    parts = filename.replace(".md", "").split("_", 3)
    if len(parts) >= 4:
        return parts[3].replace("-", " ").title()
    return filename


# Close-by-email link. Tapping in the morning brief opens a pre-filled
# mailto: with subject "close <id>"; process_close_replies.py picks it up
# via IMAP and runs `query_graph.py done <id>`. Gmail subaddressing
# (`+kbclose`) lets a filter route these without changing the inbox address.
_CLOSE_RECIPIENT = "eoinlane+kbclose@gmail.com"


def _close_link(item_id):
    return f"[close](mailto:{_CLOSE_RECIPIENT}?subject=close%20{item_id})"


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
        SELECT id, meeting_filename, text, owner, project FROM action_items
        WHERE status = 'open'
          AND (LOWER(owner) LIKE ? OR LOWER(owner) LIKE ?)
    """, (f"%{args.name.lower()}%", f"%{name_slug}%")).fetchall()

    if project:
        their_items = [r for r in their_items if (r["project"] or meeting_category(r["meeting_filename"])).upper() == project.upper()]

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
        SELECT id, meeting_filename, text, project FROM action_items
        WHERE status = 'open'
          AND LOWER(owner) LIKE '%eoin%'
    """).fetchall()

    if project:
        your_items = [r for r in your_items if (r["project"] or meeting_category(r["meeting_filename"])).upper() == project.upper()]

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
        decisions = conn.execute("SELECT meeting_filename, text, project FROM decisions").fetchall()
        decisions = [d for d in decisions if (d["project"] or meeting_category(d["meeting_filename"])).upper() == project.upper()]
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
            UPDATE action_items SET status = 'closed', closed_at = CURRENT_TIMESTAMP
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
        row = conn.execute("SELECT id, meeting_filename, text, owner, status FROM action_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            print(f"No action item with ID #{item_id}")
            conn.close()
            return
        if row["status"] == "closed":
            print(f"#{item_id} is already closed.")
            conn.close()
            return
        conn.execute("UPDATE action_items SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = ?", (item_id,))
        conn.commit()
        save_closure(row["meeting_filename"], row["text"])
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
        conn.execute("UPDATE action_items SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = ?", (r["id"],))
        conn.commit()
        save_closure(r["meeting_filename"], r["text"])
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


# Pinned model + URL from shared/config.py. Imports inline (rather than at
# the top of the module) because query_graph.py predates the shared/ layout
# and is imported via a sys.path insert further down — keeping the import
# co-located with its use avoids reordering risk.
import sys as _sys_qg
from pathlib import Path as _Path_qg
_sys_qg.path.insert(0, str(_Path_qg(__file__).resolve().parent.parent))
try:
    from shared.config import HAIKU_MODEL, OPUS_MODEL, LITELLM_URL_REMOTE as LITELLM_URL
except ImportError:
    HAIKU_MODEL = "claude-haiku-4-5"
    OPUS_MODEL = "claude-opus-4-7"
    LITELLM_URL = "http://100.121.184.27:4000/v1/chat/completions"
LITELLM_MODEL = HAIKU_MODEL  # default for non-synthesis calls; synthesis picks its own


def call_haiku(system_prompt, user_prompt, model=None):
    """Call an Anthropic model via the LiteLLM proxy. Name kept for backwards
    compatibility — earlier callers always used Haiku. Pass `model=` to override
    (e.g. OPUS_MODEL for synthesis). Opus 4.7 dropped support for `temperature`,
    so we skip it for Opus-family models and keep deterministic-output behaviour
    for everything else."""
    chosen = model or LITELLM_MODEL
    body = {
        "model": chosen,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if "opus" not in chosen.lower():
        body["temperature"] = 0
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        LITELLM_URL, data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"].strip()


def cmd_synthesise(args):
    if not args.name and not args.project:
        print("Usage: query_graph.py synthesise \"Person Name\" or synthesise --project DCC", file=sys.stderr)
        sys.exit(1)

    conn = get_conn(GRAPH_DB)
    today = _dt.date.today().isoformat()

    if args.project:
        entity_type = "project"
        entity_id = args.project.upper()
        label = entity_id
    else:
        entity_type = "person"
        entity_id = slugify(args.name)
        label = args.name

    # --- Gather all data for this entity ---

    # Meetings
    if entity_type == "person":
        meetings = conn.execute("""
            SELECT DISTINCT to_id as fn FROM graph_edges
            WHERE from_type = 'person' AND from_id = ?
              AND edge_type IN ('SPOKE_IN', 'MENTIONED_IN')
            ORDER BY to_id
        """, (entity_id,)).fetchall()
        if not meetings:
            meetings = conn.execute("""
                SELECT DISTINCT to_id as fn FROM graph_edges
                WHERE from_type = 'person' AND from_id LIKE ?
                  AND edge_type IN ('SPOKE_IN', 'MENTIONED_IN')
                ORDER BY to_id
            """, (f"%{entity_id}%",)).fetchall()
    else:
        meetings = conn.execute("""
            SELECT DISTINCT to_id as fn FROM graph_edges
            WHERE edge_type = 'PART_OF' AND to_type = 'category' AND to_id = ?
            ORDER BY from_id
        """, (entity_id,)).fetchall()
        # For projects, the meeting filename is in from_id
        meetings = conn.execute("""
            SELECT DISTINCT from_id as fn FROM graph_edges
            WHERE edge_type = 'PART_OF' AND to_type = 'category' AND to_id = ?
              AND from_type = 'meeting'
            ORDER BY from_id DESC
        """, (entity_id,)).fetchall()

    meeting_fns = [m["fn"] for m in meetings]

    if not meeting_fns:
        print(f"No meetings found for {label}")
        conn.close()
        return

    # Get summaries from KB markdown files
    kb_dir = Path.home() / "knowledge_base" / "meetings"
    summaries = []
    for fn in meeting_fns[-30:]:  # Last 30 meetings max
        md_path = kb_dir / fn
        if not md_path.exists():
            continue
        with open(md_path) as f:
            content = f.read()
        # Extract summary section
        sm = re.search(r"## Summary\n\n(.+?)(\n\n##|\Z)", content, re.DOTALL)
        if sm:
            date = meeting_date(fn)
            cat = meeting_category(fn)
            summaries.append(f"[{date} | {cat}] {sm.group(1).strip()}")

    # Action items
    if entity_type == "person":
        actions = conn.execute("""
            SELECT meeting_filename, text, status FROM action_items
            WHERE LOWER(owner) LIKE ? OR LOWER(owner) LIKE ?
            ORDER BY meeting_filename DESC LIMIT 20
        """, (f"%{args.name.lower()}%", f"%{entity_id}%")).fetchall()
    else:
        all_actions = conn.execute("""
            SELECT meeting_filename, text, owner, status, project FROM action_items
            WHERE status = 'open' ORDER BY meeting_filename DESC
        """).fetchall()
        actions = [a for a in all_actions if (a["project"] or meeting_category(a["meeting_filename"])).upper() == entity_id][:20]

    # Decisions
    all_decisions = conn.execute("SELECT meeting_filename, text, project FROM decisions ORDER BY meeting_filename DESC").fetchall()
    if entity_type == "person":
        decisions = [d for d in all_decisions if d["meeting_filename"] in meeting_fns][:15]
    else:
        decisions = [d for d in all_decisions if (d["project"] or meeting_category(d["meeting_filename"])).upper() == entity_id][:15]

    # Documents
    if entity_type == "person":
        docs = conn.execute("""
            SELECT to_id FROM graph_edges
            WHERE from_type = 'person' AND from_id = ? AND to_type = 'document'
        """, (entity_id,)).fetchall()
    else:
        docs = conn.execute("""
            SELECT from_id FROM graph_edges
            WHERE edge_type = 'PART_OF' AND to_id = ? AND from_type = 'document'
        """, (entity_id,)).fetchall()

    # Previous synthesis (for progressive compression)
    prev = conn.execute("""
        SELECT text, created_at FROM syntheses
        WHERE entity_type = ? AND entity_id = ?
        ORDER BY created_at DESC LIMIT 1
    """, (entity_type, entity_id)).fetchone()

    # --- Build the LLM prompt ---
    system = """You are a strategic advisor synthesising meeting history for a consultant called Eoin Lane.
Write a concise narrative (200-400 words) covering:
1. TRAJECTORY — how has this relationship/project evolved over time?
2. CURRENT STATE — what's active right now, what's blocked, what's the momentum?
3. KEY PEOPLE — who matters and what are their roles/positions?
4. OPEN THREADS — what's unresolved or at risk of falling through the cracks?
5. STRATEGIC NOTES — what should Eoin be thinking about for the next interaction?

Be specific with names, dates, and concrete details. No filler. This is a working document, not a report."""

    parts = [f"Synthesise the relationship/project: **{label}**\n"]
    parts.append(f"Total meetings: {len(meeting_fns)} (showing last {len(summaries)})\n")

    if prev:
        parts.append(f"--- PREVIOUS SYNTHESIS ({prev['created_at']}) ---")
        parts.append(prev["text"])
        parts.append("--- END PREVIOUS SYNTHESIS ---\n")
        parts.append("Update and compress this synthesis with the new information below.\n")

    if summaries:
        parts.append("## Meeting Summaries (chronological)")
        parts.extend(summaries[-20:])

    if actions:
        parts.append("\n## Open Action Items")
        for a in actions:
            status = a["status"]
            owner = a["owner"] if "owner" in a.keys() else ""
            parts.append(f"  [{status}] {owner}: {a['text'][:100]}")

    if decisions:
        parts.append("\n## Key Decisions")
        for d in decisions:
            date = meeting_date(d["meeting_filename"])
            parts.append(f"  [{date}] {d['text'][:100]}")

    if docs:
        parts.append(f"\n## Related Documents: {len(docs)}")

    user_prompt = "\n".join(parts)

    # --- Call the LLM ---
    # Opus is the default — synthesis is reasoning over many noisy inputs and
    # the quality gap vs Haiku is material (see A/B at 2026-05-30). Use --fast
    # to fall back to Haiku for ~20× cost reduction when the deeper read
    # isn't needed (e.g. fast iteration / draft).
    chosen_model = HAIKU_MODEL if getattr(args, "fast", False) else OPUS_MODEL
    print(f"Synthesising {entity_type}: {label} "
          f"({len(meeting_fns)} meetings, {len(actions)} actions, {len(decisions)} decisions) "
          f"with {chosen_model}...")

    try:
        result = call_haiku(system, user_prompt, model=chosen_model)
    except Exception as e:
        print(f"Error calling {chosen_model}: {e}", file=sys.stderr)
        conn.close()
        return

    # --- Store the synthesis (record which model produced it) ---
    conn.execute(
        "INSERT INTO syntheses (entity_type, entity_id, text, model) VALUES (?, ?, ?, ?)",
        (entity_type, entity_id, result, chosen_model)
    )
    conn.commit()

    print(f"\n# Synthesis: {label}")
    print(f"Generated: {today} | {len(meeting_fns)} meetings | {len(actions)} action items | {len(decisions)} decisions\n")
    print(result)
    print()

    conn.close()


def cmd_tags(args):
    conn = get_conn(GRAPH_DB)

    if args.search:
        # Search for meetings by tag
        search = args.search.lower()
        concepts = conn.execute(
            "SELECT id, label, mention_count, category FROM concepts WHERE LOWER(label) LIKE ? ORDER BY mention_count DESC",
            (f"%{search}%",)
        ).fetchall()

        if not concepts:
            print(f"No tags matching \"{args.search}\"")
            conn.close()
            return

        for c in concepts[:10]:
            print(f"## {c['label']} ({c['mention_count']} mentions, {c['category']})\n")
            # Find meetings that discussed this concept
            meetings = conn.execute("""
                SELECT from_id FROM graph_edges
                WHERE edge_type = 'DISCUSSED' AND to_type = 'concept' AND to_id = ?
                ORDER BY from_id DESC
            """, (str(c["id"]),)).fetchall()
            for m in meetings[:10]:
                fn = m["from_id"]
                date = meeting_date(fn)
                cat = meeting_category(fn)
                title = meeting_title(fn)
                print(f"  {date} | {cat} | {title}")
            if len(meetings) > 10:
                print(f"  ... and {len(meetings) - 10} more")
            print()
    else:
        # Show top tags
        project = args.project
        if project:
            concepts = conn.execute(
                "SELECT label, mention_count, category FROM concepts WHERE UPPER(category) = ? ORDER BY mention_count DESC LIMIT 30",
                (project.upper(),)
            ).fetchall()
        else:
            concepts = conn.execute(
                "SELECT label, mention_count, category FROM concepts ORDER BY mention_count DESC LIMIT 30"
            ).fetchall()

        proj_label = f" [{project.upper()}]" if project else ""
        print(f"## Top Tags{proj_label}\n")
        for c in concepts:
            print(f"  {c['mention_count']:>3}x  {c['label']} ({c['category']})")

    conn.close()


def cmd_focus(args):
    """Curated focus list — Eoin-owned action items, fresh, deduped against
    closures, capped per-project and overall. The Apple Reminders push isn't
    wired in yet; this command prints what *would* be pushed so the curation
    rules can be tuned before any reminders are written.

    Defaults: 4-week freshness window, max 3 items per project, hard cap 10
    overall. The "Today" set is the top 3 across all picks by recording date.

    Quality filters (off when --no-quality-filter):
      - Drops projects in --exclude (default: other:personal,FutureBusiness).
      - Drops items whose action text starts with a weak verb (Discuss,
        Provide, Consider, Explore) or contains summary-boilerplate
        ("post-meeting summary", "summary of the meeting").
    """
    today = _dt.date.today()
    cutoff = today - _dt.timedelta(weeks=args.weeks)

    # Project exclude list: comma-separated, case-insensitive.
    excluded_projects = {p.strip().lower() for p in (args.exclude or "").split(",") if p.strip()}

    # Content quality heuristics.
    WEAK_VERBS = {"discuss", "provide", "consider", "explore", "think"}
    BOILERPLATE_FRAGMENTS = (
        "post-meeting summary",
        "summary of the meeting",
        "send out a summary",
        "send the summary",
    )

    def is_low_quality(text):
        t = text.strip().lower()
        first = t.split()[0] if t else ""
        if first in WEAK_VERBS:
            return True
        for frag in BOILERPLATE_FRAGMENTS:
            if frag in t:
                return True
        return False

    # Closures dedupe — same key shape as save_closure().
    closures = {}
    if CLOSURES_FILE.exists():
        try:
            with open(CLOSURES_FILE) as f:
                closures = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    conn = get_conn(GRAPH_DB)
    rows = conn.execute(
        "SELECT id, meeting_filename, text, owner, project "
        "FROM action_items WHERE status = 'open' AND owner LIKE '%eoin%' COLLATE NOCASE"
    ).fetchall()

    # Filter: drop stale, drop already-closed, drop excluded projects, drop low quality.
    dropped_quality = 0
    dropped_excluded = 0
    candidates = []
    for r in rows:
        date_str = r["meeting_filename"].split("_")[0] if "_" in r["meeting_filename"] else None
        try:
            mtg_date = _dt.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        if mtg_date < cutoff:
            continue
        key = f"{r['meeting_filename']}::{r['text'][:80]}"
        if closures.get(key) == "closed":
            continue
        project = (r["project"] or meeting_category(r["meeting_filename"]) or "other")
        if project.lower() in excluded_projects:
            dropped_excluded += 1
            continue
        if not args.no_quality_filter and is_low_quality(r["text"] or ""):
            dropped_quality += 1
            continue
        candidates.append({
            "id": r["id"],
            "filename": r["meeting_filename"],
            "text": r["text"],
            "project": project,
            "date": mtg_date,
        })

    # Optional project filter.
    if args.project:
        proj = args.project.upper()
        candidates = [c for c in candidates if c["project"].upper() == proj]

    # Group by project, newest first within project.
    by_project = {}
    for c in candidates:
        by_project.setdefault(c["project"], []).append(c)
    for p in by_project:
        by_project[p].sort(key=lambda x: x["date"], reverse=True)

    # Round-robin pick: max 3 per project, hard cap on total.
    per_project_cap = 3
    picks = []
    project_keys = sorted(by_project.keys())
    cursors = {p: 0 for p in project_keys}
    while len(picks) < args.max:
        added = False
        for p in project_keys:
            if cursors[p] < min(per_project_cap, len(by_project[p])):
                picks.append(by_project[p][cursors[p]])
                cursors[p] += 1
                added = True
                if len(picks) >= args.max:
                    break
        if not added:
            break

    # Today set: top 3 picks by date.
    today_set = sorted(picks, key=lambda x: x["date"], reverse=True)[:3]
    today_ids = {p["id"] for p in today_set}

    # ── Output ───────────────────────────────────────────────────────────────
    proj_label = f" [{args.project.upper()}]" if args.project else ""
    print(f"## Focus list{proj_label} — {today.isoformat()}\n")
    print(f"Source: graph.db open items, owner=Eoin Lane, recorded "
          f"{cutoff.isoformat()} → {today.isoformat()}, not in graph_closures.json.")
    if excluded_projects:
        print(f"Excluded projects: {', '.join(sorted(excluded_projects))} ({dropped_excluded} dropped)")
    if not args.no_quality_filter and dropped_quality:
        print(f"Quality filter dropped: {dropped_quality} items (weak verbs / summary boilerplate)")
    print(f"Candidates after filter: {len(candidates)}")
    print(f"Picked: {len(picks)} (cap {args.max}, max {per_project_cap}/project)\n")
    if not args.push:
        print("**DRY RUN — nothing pushed to Apple Reminders.** (Pass --push to write.)\n")
    else:
        print("**PUSH MODE — reminders will be created in Apple Reminders.**\n")

    if not picks:
        print("(no items match)")
        conn.close()
        return

    # "Today" cross-cut.
    print(f"### Today ★ ({len(today_set)})\n")
    for item in today_set:
        print(f"- [ ] {item['text']}")
        print(f"      _{item['project']} · {item['date'].isoformat()} · "
              f"{meeting_title(item['filename'])}_\n")

    # Per-project breakdown.
    picks_by_project = {}
    for item in picks:
        picks_by_project.setdefault(item["project"], []).append(item)
    for proj in sorted(picks_by_project.keys()):
        items = picks_by_project[proj]
        print(f"### {proj} ({len(items)})\n")
        for item in items:
            tag = "  ★" if item["id"] in today_ids else ""
            print(f"- [ ] {item['text']}{tag}")
            print(f"      _{item['date'].isoformat()} · {meeting_title(item['filename'])}_\n")

    # What got dropped (truncated).
    dropped_by_project = {}
    for p, items in by_project.items():
        if len(items) > per_project_cap:
            dropped_by_project[p] = len(items) - per_project_cap
    if dropped_by_project:
        print("### Not surfaced (next batch / not enough room)\n")
        for p in sorted(dropped_by_project.keys()):
            print(f"- {p}: +{dropped_by_project[p]} more candidates")
        print()

    if args.push:
        _push_to_reminders(picks, today_ids)

    conn.close()


def _push_to_reminders(picks, today_ids):
    """Push picks into Apple Reminders. Items in today_ids also land in KB:Today.

    Dedupes by an embedded ``[kb-id]`` line in each reminder's notes — so
    re-running ``focus --push`` is safe and won't create duplicates.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import apple_reminders as ar

    def kb_id(item):
        # rstrip so a trailing space inside the [:80] window doesn't make
        # round-tripped ids miss the dedupe set (Reminders preserves the
        # space, but our parser strips on read).
        return f"{item['filename']}::{item['text'][:80].rstrip()}"

    def notes_for(item):
        return (
            f"{item['date'].isoformat()} · {meeting_title(item['filename'])}\n\n"
            f"[kb-id] {kb_id(item)}"
        )

    def existing_ids_in(list_name):
        try:
            existing = ar.list_reminders(list_name, include_completed=False)
        except RuntimeError:
            return set()
        ids = set()
        for r in existing:
            body = (r.get("body") or "").strip()
            for line in body.splitlines():
                if line.startswith("[kb-id] "):
                    ids.add(line[len("[kb-id] "):].strip())
        return ids

    # What lists we need: one per project + KB:Today if any today picks exist.
    projects = sorted({p["project"] for p in picks})
    list_names = [f"KB:{p}" for p in projects]
    today_picks = [p for p in picks if p["id"] in today_ids]
    if today_picks:
        list_names.append("KB:Today")

    print("### Push results\n")
    for ln in list_names:
        result = ar.ensure_list(ln)
        verb = "created" if result.get("created") else "exists"
        print(f"- list `{ln}` ({verb})")

    # KB:Today is a rolling snapshot — prune entries that aren't in the
    # current today set before pushing, so it always reflects "right now".
    # Per-project lists are append-only; closures handle their lifecycle.
    pruned = 0
    if today_picks:
        current_today_ids = {kb_id(p) for p in today_picks}
        for r in ar.list_reminders("KB:Today", include_completed=False):
            body = (r.get("body") or "")
            for line in body.splitlines():
                if line.startswith("[kb-id] "):
                    rid = line[len("[kb-id] "):].strip()
                    if rid not in current_today_ids:
                        ar.delete_reminder(r["id"])
                        pruned += 1
                    break
    if pruned:
        print(f"- pruned {pruned} stale entr{'y' if pruned == 1 else 'ies'} from KB:Today")

    # One existing-id snapshot per list — fetched once before any creates.
    existing_by_list = {ln: existing_ids_in(ln) for ln in list_names}

    created = 0
    skipped = 0
    for item in picks:
        targets = [f"KB:{item['project']}"]
        if item["id"] in today_ids:
            targets.append("KB:Today")
        for ln in targets:
            if kb_id(item) in existing_by_list[ln]:
                skipped += 1
                continue
            ar.create_reminder(ln, item["text"], notes_for(item))
            existing_by_list[ln].add(kb_id(item))
            created += 1

    print(f"\nCreated: {created} reminder(s). Skipped (already present): {skipped}.\n")


_BRIEF_EXCLUDED_CALENDARS = {"cal_personal.txt", "cal_home.txt"}

# Calendar events to skip for the coverage check — room bookings, holds, wellness,
# all-staff non-meeting events. These don't generate recordings and shouldn't be
# flagged as "missing transcripts". Keep the list conservative — a false-positive
# in the gap report is annoying but recoverable; a false-negative hides real misses.
_BRIEF_NONMEETING_TITLES = {"Room", "test", "Hold", "Block", "Lunch"}
_BRIEF_NONMEETING_KEYWORDS = (
    "wellness", "menopause", "sleep for restorative",
    "out of office", "ooo", "annual leave", "holiday",
    "do not book", "blocked",
)

_STUCK_RECORDINGS_FILE = "/Users/eoin/.local/share/kb/stuck_recordings.txt"


def _is_nonmeeting_event(evt):
    """Return True if a calendar event isn't a real meeting (doesn't need a
    recording). Conservative — only filters obvious non-meetings."""
    if evt["start_time"] == "all-day":
        return True
    title = evt["title"]
    if title in _BRIEF_NONMEETING_TITLES:
        return True
    title_l = title.lower()
    for kw in _BRIEF_NONMEETING_KEYWORDS:
        if kw in title_l:
            return True
    return False


def _recording_minutes_for_date(d):
    """Return list of meeting-recording start times (minutes-since-midnight) on
    date d, parsed from KB meeting filenames of the form
    YYYY-MM-DD_HHMM*_CATEGORY_slug.md. Empty list if KB not built yet for d."""
    from pathlib import Path
    kb_meetings = Path(os.path.expanduser("~/knowledge_base/meetings"))
    if not kb_meetings.is_dir():
        return []
    prefix = d.strftime("%Y-%m-%d_")
    mins = []
    for f in kb_meetings.glob(f"{prefix}*.md"):
        parts = f.stem.split("_")
        if len(parts) >= 2 and len(parts[1]) >= 4 and parts[1][:4].isdigit():
            hh = int(parts[1][:2])
            mm = int(parts[1][2:4])
            mins.append(hh * 60 + mm)
    return mins


def _meetings_without_recordings(days_back=7, window_minutes=90):
    """For the last `days_back` days (excluding today), find calendar events with
    no matching KB recording within ±window_minutes of their start time. Catches
    Stage A failures (iPhone export shortcut didn't fire) and forgotten Plaud
    recordings within hours instead of days."""
    today = _dt.date.today()
    gaps = []
    for d_offset in range(1, days_back + 1):
        d = today - _dt.timedelta(days=d_offset)
        events = _parse_calendar_events_on(d)
        if not events:
            continue
        recording_mins = _recording_minutes_for_date(d)
        for evt in events:
            if _is_nonmeeting_event(evt):
                continue
            try:
                eh, em = evt["start_time"].split(":")
                evt_min = int(eh) * 60 + int(em)
            except (ValueError, AttributeError):
                continue
            if not any(abs(rmin - evt_min) <= window_minutes for rmin in recording_mins):
                gaps.append((d, evt))
    return gaps


def _parse_calendar_events_on(target_date):
    """Read all calendar exports, return events on target_date.
    Returns list of dicts: {title, start_time, end_time, attendees}.
    Calendar files live at ~/.local/share/kb/calendars/cal_*.txt, exported by icalBuddy.
    Blocks are separated by lines containing only '---'. Each block has
    TITLE: / START: / END: / ATTENDEES: lines.
    Personal and Home calendars are excluded — scouting, family, and other
    non-work events shouldn't pollute the morning brief."""
    import os, glob
    cal_dir = os.path.expanduser("~/.local/share/kb/calendars")
    target_str = target_date.strftime("%d %B %Y")
    events = []
    seen = set()
    for cal_file in sorted(glob.glob(os.path.join(cal_dir, "cal_*.txt"))):
        if os.path.basename(cal_file) in _BRIEF_EXCLUDED_CALENDARS:
            continue
        try:
            content = open(cal_file).read()
        except OSError:
            continue
        for block in content.split("\n---\n"):
            block = block.strip()
            if not block:
                continue
            m_start = re.search(r"^START:\s*(.+)$", block, re.M)
            if not m_start:
                continue
            start_line = m_start.group(1).strip()
            if target_str not in start_line:
                continue
            m_title = re.search(r"^TITLE:\s*(.+)$", block, re.M)
            title = m_title.group(1).strip() if m_title else "(no title)"
            m_start_time = re.search(r"at\s+(\d{1,2}:\d{2})", start_line)
            start_time = m_start_time.group(1) if m_start_time else "all-day"
            m_end = re.search(r"^END:\s*(.+)$", block, re.M)
            end_time = ""
            if m_end:
                m_endtime = re.search(r"(\d{1,2}:\d{2})", m_end.group(1))
                end_time = m_endtime.group(1) if m_endtime else ""
            m_att = re.search(r"^ATTENDEES:\s*(.+)$", block, re.M)
            attendees = []
            if m_att:
                attendees = [a.strip() for a in m_att.group(1).split("|") if a.strip()]
            key = (title, start_time)
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "title": title,
                "start_time": start_time,
                "end_time": end_time,
                "attendees": attendees,
            })
    events.sort(key=lambda e: e["start_time"] if ":" in e["start_time"] else "z")
    return events


def _last_open_item_for_attendee(attendee, conn):
    """Look up the most recent open action item where this person is the owner.
    Returns (meeting_filename, text) or None. Tries email-local-part, full name,
    and first-name fallbacks against fuzzy owner match."""
    if "@" in attendee:
        candidate = attendee.split("@")[0].replace(".", " ").replace("_", " ").strip()
    else:
        candidate = attendee.strip()
    tries = [candidate]
    if " " in candidate:
        tries.append(candidate.split()[0])  # first name
        tries.append(candidate.split()[-1])  # last name
    seen = set()
    for q in tries:
        q_l = q.lower()
        if q_l in seen or len(q_l) < 3 or "eoin" in q_l:
            continue
        seen.add(q_l)
        row = conn.execute("""
            SELECT meeting_filename, text FROM action_items
            WHERE status = 'open' AND LOWER(owner) LIKE ?
            ORDER BY meeting_filename DESC LIMIT 1
        """, (f"%{q_l}%",)).fetchone()
        if row:
            return row
    return None


def _clean_attendee(att):
    """Clean a calendar attendee string for brief display.
    Returns (display_name, is_valid). is_valid=False means this entry is
    calendar-export noise (room codes, group markers, prefix garbage) and
    should be dropped from the brief entirely — including from the per-
    attendee commitment lookup, since looking up "PS4" or "All in NTA"
    only produces false-positive matches against action_items.owner."""
    if not att:
        return ("", False)
    a = att.strip()
    # Colon-prefix garbage from calendar export
    if a.startswith(":") or a.startswith("To:") or a.lower().startswith("subject:"):
        return ("", False)
    a_low = a.lower()
    # Room codes and group markers — not people
    if a_low in {"hr", "ps4", "ps5", "ps6", "ps7"}:
        return ("", False)
    if a_low.startswith("all in ") or a_low.startswith("all hands"):
        return ("", False)
    # Malformed angle-bracket entries (export bug — opening < without closing >)
    if "<" in a and ">" not in a:
        return ("", False)
    # Strip @domain from emails
    if "@" in a:
        a = a.split("@")[0]
    # Convert "first.last" → "First Last"
    if "." in a and " " not in a:
        parts = a.split(".")
        if all(p.replace("_", "").isalpha() and p for p in parts):
            a = " ".join(p.capitalize() for p in parts)
    # Single lowercase token → titlecase (imperfect but better than raw)
    elif a.islower() and a.replace("_", "").isalpha():
        a = a.capitalize()
    return (a, True)


def cmd_brief(args):
    """Daily morning brief. Designed for 06:30 launchd run. Outputs markdown to stdout.
    Sections: today's meetings with per-attendee last commitment, your open items
    (last 2 weeks), others' open items owed to you (oldest first)."""
    today = _dt.date.today()
    conn = get_conn(GRAPH_DB)

    print(f"# Morning brief — {today.strftime('%A %d %B %Y')}")
    print()

    events = _parse_calendar_events_on(today)
    if events:
        print(f"## Today ({len(events)} meeting{'s' if len(events) != 1 else ''})")
        print()
        for evt in events:
            time_str = evt["start_time"]
            if evt["end_time"]:
                time_str += f"–{evt['end_time']}"
            print(f"### {time_str}  {evt['title']}")
            # Filter Eoin + clean attendees via _clean_attendee (drops
            # PS4/HR/All in NTA/To:X/etc., title-cases email-prefix names).
            cleaned = []
            for a in evt["attendees"]:
                if not a or "eoin" in a.lower():
                    continue
                display, valid = _clean_attendee(a)
                if valid:
                    cleaned.append((a, display))
            if cleaned:
                print(f"_With:_ {', '.join(d for _, d in cleaned)}")
                print()
                shown = 0
                for raw, display in cleaned:
                    if shown >= 6:
                        break
                    last = _last_open_item_for_attendee(raw, conn)
                    if last:
                        fn, text = last
                        if len(text) > 160:
                            text = text[:160].rstrip() + "…"
                        print(f"- **{display}** owes since {meeting_date(fn)}: {text}")
                        shown += 1
            print()
    else:
        print("## Today")
        print()
        print("No calendar meetings scheduled.")
        print()

    # --- Pipeline gaps: stuck 0-byte placeholders + missing recordings ---
    # Both sections surface Stage A failures (Apple Notes export step didn't
    # fire, or recording sat in Notes data store and never reached iCloud).
    # Catching these within hours instead of days; the 27 May Alex catch-up
    # incident motivated this — the recording was in Notes for ~36h before
    # being discovered.
    try:
        if os.path.exists(_STUCK_RECORDINGS_FILE):
            with open(_STUCK_RECORDINGS_FILE) as f:
                stuck = [line.strip() for line in f if line.strip()]
            if stuck:
                print(f"## ⚠ Stuck 0-byte recordings ({len(stuck)})")
                print()
                print("_Apple Notes recordings sitting in iCloud as empty placeholders for >24h. "
                      "Likely a failed export — either trigger the iPhone shortcut again or "
                      "delete the placeholder._")
                print()
                for line in stuck:
                    parts = line.split("|")
                    fname = parts[0]
                    age_h = ""
                    if len(parts) >= 3 and parts[2].isdigit():
                        age_h = f" (stuck {int(parts[2]) // 3600}h)"
                    print(f"- `{fname}`{age_h}")
                print()
    except OSError:
        pass

    gaps = _meetings_without_recordings(days_back=7)
    if gaps:
        print(f"## ⚠ Meetings without recordings (last 7 days, {len(gaps)})")
        print()
        print("_Calendar events with no matching transcript within ±90 min. "
              "Check whether the meeting actually happened, the recording is "
              "stuck in Apple Notes, or you forgot to record._")
        print()
        for d, evt in gaps:
            cleaned = []
            for a in evt.get("attendees", []):
                if not a or "eoin" in a.lower():
                    continue
                disp, ok = _clean_attendee(a)
                if ok:
                    cleaned.append(disp)
            att_str = f" — _With:_ {', '.join(cleaned[:3])}" if cleaned else ""
            print(f"- **{d.strftime('%a %d %b')} {evt['start_time']}** — {evt['title']}{att_str}")
        print()

    cutoff_2w = (today - _dt.timedelta(weeks=2)).isoformat()
    cutoff_4w = (today - _dt.timedelta(weeks=4)).isoformat()

    # --- Closed in the last 24h (new section, depends on closed_at column) ---
    # Show items the user actually knocked off recently so the brief reflects
    # closure activity, not just open backlog. Bounded to last 24h via the
    # closed_at timestamp written by cmd_done (and propagated by build_graph
    # from .graph_closures.json).
    yesterday_start = (today - _dt.timedelta(days=1)).isoformat()
    try:
        closed_recently = conn.execute("""
            SELECT meeting_filename, owner, text, closed_at FROM action_items
            WHERE status = 'closed'
              AND closed_at IS NOT NULL
              AND closed_at >= ?
            ORDER BY closed_at DESC LIMIT 10
        """, (yesterday_start,)).fetchall()
    except sqlite3.OperationalError:
        closed_recently = []  # closed_at column not yet present; safe fallback
    if closed_recently:
        print(f"## Closed in the last 24h ({len(closed_recently)})")
        print()
        for fn, owner, text, _ts in closed_recently:
            owner_disp = owner or "you"
            if len(text) > 160:
                text = text[:160].rstrip() + "…"
            print(f"- [{meeting_date(fn)}] **{owner_disp}**: {text}")
        print()

    your_items = conn.execute("""
        SELECT id, meeting_filename, text FROM action_items
        WHERE status = 'open'
          AND LOWER(owner) LIKE '%eoin%'
          AND meeting_filename >= ?
        ORDER BY meeting_filename DESC LIMIT 10
    """, (cutoff_2w,)).fetchall()

    if your_items:
        print(f"## Your open commitments (last 2 weeks, {len(your_items)} shown)")
        print()
        for item_id, fn, text in your_items:
            cat = meeting_category(fn)
            if len(text) > 180:
                text = text[:180].rstrip() + "…"
            print(f"- [{meeting_date(fn)} {cat}] {text} · {_close_link(item_id)}")
        print()

    others = conn.execute("""
        SELECT meeting_filename, owner, text FROM action_items
        WHERE status = 'open'
          AND owner IS NOT NULL
          AND LOWER(owner) NOT LIKE '%eoin%'
          AND meeting_filename >= ?
          AND meeting_filename < ?
        ORDER BY meeting_filename ASC LIMIT 12
    """, (cutoff_4w, cutoff_2w)).fetchall()

    if others:
        print("## Others owe you (2–4 weeks old, oldest first)")
        print()
        for fn, owner, text in others:
            if len(text) > 150:
                text = text[:150].rstrip() + "…"
            print(f"- [{meeting_date(fn)}] **{owner}**: {text}")
        print()

    conn.close()


def cmd_stale_nudge(args):
    """Weekly stale-commitment nudge. Designed for Friday 06:30 launchd run.
    Shows YOUR open commitments older than N weeks — the items the daily
    morning brief misses because it only looks back 2 weeks. Grouped by
    project, top per_project oldest per project, hard cap total."""
    today = _dt.date.today()
    weeks = getattr(args, "weeks", 3)
    per_project = getattr(args, "per_project", 3)
    cap = getattr(args, "cap", 15)
    cutoff = (today - _dt.timedelta(weeks=weeks)).isoformat()
    conn = get_conn(GRAPH_DB)
    rows = conn.execute("""
        SELECT id, meeting_filename, text FROM action_items
        WHERE status = 'open'
          AND LOWER(owner) LIKE '%eoin%'
          AND meeting_filename < ?
        ORDER BY meeting_filename ASC
    """, (cutoff,)).fetchall()

    print(f"# Stale commitments — {today.strftime('%A %d %B %Y')}")
    print()

    if not rows:
        print(f"No open Eoin-owned items older than {weeks} weeks. Inbox zero.")
        conn.close()
        return

    by_proj = {}
    for item_id, fn, text in rows:
        cat = meeting_category(fn)
        by_proj.setdefault(cat, []).append((item_id, fn, text))

    total_stale = len(rows)
    print(f"_{total_stale} of your open items are older than {weeks} weeks. "
          f"Showing top {per_project} per project (cap {cap})._")
    print()
    print("For each: **do it**, tap **close** beside the item, or explicitly defer.")
    print()

    shown_total = 0
    for proj in sorted(by_proj, key=lambda p: -len(by_proj[p])):
        if shown_total >= cap:
            break
        items = by_proj[proj][:per_project]
        print(f"## {proj} ({len(by_proj[proj])} stale total)")
        print()
        for item_id, fn, text in items:
            if shown_total >= cap:
                break
            date_s = meeting_date(fn)
            try:
                age_weeks = (today - _dt.date.fromisoformat(date_s)).days // 7
                age_str = f"{age_weeks}w old"
            except ValueError:
                age_str = "?"
            if len(text) > 200:
                text = text[:200].rstrip() + "…"
            print(f"- [{date_s} · {age_str}] {text} · {_close_link(item_id)}")
            shown_total += 1
        print()

    if total_stale > shown_total:
        print(f"_… and {total_stale - shown_total} more stale items not shown. "
              f"`query_graph.py open --person 'Eoin Lane'` for the full list._")

    conn.close()


def cmd_review(args):
    conn = get_conn(GRAPH_DB)
    today = _dt.date.today()
    # Default to a rolling 7-day window (today minus 7 days). Previously this
    # computed `today - days=today.weekday()` which on a Tuesday returned only
    # the Mon-Tue slice — broken-by-default for interactive review since it
    # excluded the previous week's meetings. The launchd Mon-07:00 invocation
    # works around this by passing --weeks 2; now interactive defaults work.
    weeks_back = args.weeks or 1
    full = getattr(args, "full", False)
    def _t(s, n):
        return s if full else s[:n]
    week_start = today - _dt.timedelta(days=7 * weeks_back - 1)
    week_end = today + _dt.timedelta(days=1)
    start_str = week_start.isoformat()
    end_str = week_end.isoformat()
    prev_start = (week_start - _dt.timedelta(weeks=4)).isoformat()

    print(f"# Weekly Review: {week_start.strftime('%d %b')} — {today.strftime('%d %b %Y')}")
    print()

    # --- 1. Meetings this period, by project ---
    all_meetings = conn.execute("""
        SELECT DISTINCT to_id FROM graph_edges
        WHERE to_type = 'meeting' AND edge_type = 'PART_OF'
          AND to_id >= ? AND to_id < ?
    """, (start_str, end_str)).fetchall()

    # Actually simpler: use meeting filenames from action_items + decisions
    meeting_fns = set()
    for row in conn.execute("SELECT DISTINCT meeting_filename FROM action_items WHERE meeting_filename >= ? AND meeting_filename < ?", (start_str, end_str)):
        meeting_fns.add(row[0])
    for row in conn.execute("SELECT DISTINCT meeting_filename FROM decisions WHERE meeting_filename >= ? AND meeting_filename < ?", (start_str, end_str)):
        meeting_fns.add(row[0])

    # Also get meetings from edges
    for row in conn.execute("SELECT DISTINCT to_id FROM graph_edges WHERE edge_type='PART_OF' AND to_id >= ? AND to_id < ?", (start_str, end_str)):
        meeting_fns.add(row[0])

    by_proj = {}
    for fn in meeting_fns:
        cat = meeting_category(fn)
        by_proj.setdefault(cat, []).append(fn)

    print(f"## Meetings ({len(meeting_fns)})")
    print()
    for proj in sorted(by_proj, key=lambda p: -len(by_proj[p])):
        meetings = sorted(by_proj[proj])
        print(f"  **{proj}** ({len(meetings)})")
        for fn in meetings:
            date = meeting_date(fn)
            title = meeting_title(fn)
            print(f"    {date} — {title}")
    print()

    # --- 2. Your new action items this period ---
    your_items = conn.execute("""
        SELECT meeting_filename, text FROM action_items
        WHERE status = 'open' AND LOWER(owner) LIKE '%eoin%'
          AND meeting_filename >= ? AND meeting_filename < ?
        ORDER BY meeting_filename DESC
    """, (start_str, end_str)).fetchall()

    if your_items:
        print(f"## You Committed To ({len(your_items)})")
        print()
        for r in your_items:
            date = meeting_date(r["meeting_filename"])
            cat = meeting_category(r["meeting_filename"])
            print(f"  - [ ] {_t(r['text'], 90)} ({date}, {cat})")
        print()

    # --- 3. Others' new action items this period ---
    others_items = conn.execute("""
        SELECT owner, meeting_filename, text FROM action_items
        WHERE status = 'open'
          AND LOWER(owner) NOT LIKE '%eoin%'
          AND owner NOT LIKE 'SPEAKER%' AND owner != 'unknown'
          AND owner IS NOT NULL AND owner != ''
          AND meeting_filename >= ? AND meeting_filename < ?
        ORDER BY owner, meeting_filename DESC
    """, (start_str, end_str)).fetchall()

    if others_items:
        by_owner = {}
        for r in others_items:
            by_owner.setdefault(r["owner"], []).append(r)
        print(f"## Others Committed To ({len(others_items)})")
        print()
        for owner in sorted(by_owner):
            items = by_owner[owner]
            print(f"  **{owner}** ({len(items)})")
            for r in items:
                date = meeting_date(r["meeting_filename"])
                print(f"    - [ ] {_t(r['text'], 90)} ({date})")
        print()

    # --- 4. Decisions made this period ---
    decisions = conn.execute("""
        SELECT meeting_filename, text FROM decisions
        WHERE meeting_filename >= ? AND meeting_filename < ?
        ORDER BY meeting_filename DESC
    """, (start_str, end_str)).fetchall()

    if decisions:
        print(f"## Decisions Made ({len(decisions)})")
        print()
        for d in decisions[:20]:
            date = meeting_date(d["meeting_filename"])
            cat = meeting_category(d["meeting_filename"])
            print(f"  - {_t(d['text'], 100)} ({date}, {cat})")
        if len(decisions) > 20:
            print(f"  ... and {len(decisions) - 20} more")
        print()

    # --- 5. Overdue items (open, from 2-8 weeks ago) ---
    overdue_start = (today - _dt.timedelta(weeks=8)).isoformat()
    overdue_end = (today - _dt.timedelta(weeks=2)).isoformat()
    overdue = conn.execute("""
        SELECT id, meeting_filename, text, owner FROM action_items
        WHERE status = 'open' AND LOWER(owner) LIKE '%eoin%'
          AND meeting_filename >= ? AND meeting_filename < ?
        ORDER BY meeting_filename
    """, (overdue_start, overdue_end)).fetchall()

    if overdue:
        print(f"## Overdue (2-8 weeks old, {len(overdue)} items)")
        print()
        for r in overdue[:15]:
            date = meeting_date(r["meeting_filename"])
            cat = meeting_category(r["meeting_filename"])
            age_days = (today - _dt.date.fromisoformat(date)).days
            print(f"  - [ ] {_t(r['text'], 80)} ({date}, {cat}, {age_days}d ago, #{r['id']})")
        if len(overdue) > 15:
            print(f"  ... and {len(overdue) - 15} more")
        print()

    # --- 6. People gone quiet ---
    # Find people with >5 meeting edges whose most recent meeting is >3 weeks old
    quiet_cutoff = (today - _dt.timedelta(weeks=3)).isoformat()
    quiet = conn.execute("""
        SELECT from_id, COUNT(*) as edges, MAX(to_id) as last_meeting
        FROM graph_edges
        WHERE from_type = 'person'
          AND edge_type IN ('SPOKE_IN', 'MENTIONED_IN')
          AND from_id != 'eoin-lane'
        GROUP BY from_id
        HAVING edges > 8 AND last_meeting < ?
        ORDER BY edges DESC
    """, (quiet_cutoff,)).fetchall()

    if quiet:
        print(f"## Gone Quiet (not seen in 3+ weeks)")
        print()
        for r in quiet[:10]:
            last_date = meeting_date(r["last_meeting"])
            name = r["from_id"].replace("-", " ").title()
            days_ago = (today - _dt.date.fromisoformat(last_date)).days
            print(f"  - {name} — last seen {last_date} ({days_ago}d ago, {r['edges']} meetings total)")
        print()

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
    p_open.add_argument("--by-date", action="store_true",
                        help="Use strict date-desc ordering (legacy). Default is priority-weighted "
                             "(item age × relationship recency).")

    p_done = sub.add_parser("done", help="Mark action items as done")
    p_done.add_argument("target", nargs="?", help="Action item ID or search text")
    p_done.add_argument("--stale", type=int, help="Close all items older than N weeks")

    p_dec = sub.add_parser("decisions", help="List decisions")
    p_dec.add_argument("--project", "-p", help="Filter by project/category")

    p_hist = sub.add_parser("history", help="Meeting history with a person")
    p_hist.add_argument("name", nargs="?", help="Person name")
    p_hist.add_argument("--limit", "-n", type=int, default=10)

    p_ctx = sub.add_parser("context", help="Compact context block for a person — load before drafting outbound email/chat")
    p_ctx.add_argument("name", nargs="?", help="Person name")

    p_tags = sub.add_parser("tags", help="Browse and search tags/concepts")
    p_tags.add_argument("search", nargs="?", help="Search for a tag")
    p_tags.add_argument("--project", "-p", help="Filter by project/category")

    p_synth = sub.add_parser("synthesise", help="Progressive summarisation for a person or project")
    p_synth.add_argument("name", nargs="?", help="Person name")
    p_synth.add_argument("--project", "-p", help="Synthesise a project instead of a person")
    p_synth.add_argument("--fast", action="store_true",
                         help="Use Haiku (cheap, fast, shallower) instead of the Opus default")

    p_brief = sub.add_parser("brief", help="Daily morning brief (launchd 06:30 friendly)")

    p_stale = sub.add_parser("stale-nudge", help="Weekly Friday nudge: Eoin's open commitments older than N weeks")
    p_stale.add_argument("--weeks", type=int, default=3, help="Age threshold in weeks (default 3)")
    p_stale.add_argument("--per-project", type=int, default=3, help="Max items per project (default 3)")
    p_stale.add_argument("--cap", type=int, default=15, help="Hard cap on total items shown (default 15)")

    p_review = sub.add_parser("review", help="Weekly review digest")
    p_review.add_argument("--weeks", "-w", type=int, default=1, help="How many weeks back (default: current week)")
    p_review.add_argument("--full", action="store_true", help="Don't truncate action item / decision text (useful for markdown digests)")

    p_stats = sub.add_parser("stats", help="Graph stats overview")

    p_focus = sub.add_parser("focus", help="Curated focus list ready for Apple Reminders push (dry-run only for now)")
    p_focus.add_argument("--project", "-p", help="Filter to one project")
    p_focus.add_argument("--max", type=int, default=10, help="Hard cap on total items (default 10)")
    p_focus.add_argument("--weeks", type=int, default=4, help="Freshness window in weeks (default 4)")
    p_focus.add_argument("--exclude", default="other:personal,FutureBusiness",
                        help="Comma-separated projects to exclude (default: other:personal,FutureBusiness)")
    p_focus.add_argument("--no-quality-filter", action="store_true",
                        help="Disable the weak-verb / summary-boilerplate quality filter")
    p_focus.add_argument("--push", action="store_true",
                        help="Actually create reminders in Apple Reminders (default is dry-run). "
                             "Lists are KB:<project> + KB:Today; existing entries with matching kb-id are skipped.")

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
    elif args.command == "context":
        cmd_context(args)
    elif args.command == "tags":
        cmd_tags(args)
    elif args.command == "synthesise":
        cmd_synthesise(args)
    elif args.command == "brief":
        cmd_brief(args)
    elif args.command == "stale-nudge":
        cmd_stale_nudge(args)
    elif args.command == "review":
        cmd_review(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "focus":
        cmd_focus(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
