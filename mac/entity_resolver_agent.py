#!/usr/bin/env python3
"""
entity_resolver_agent.py — LLM judgment layer for contacts.db merge_suggestions.

For each pending suggestion that hasn't been judged yet, gathers context
about both names (orgs, meeting counts, top categories, recent meetings,
co-attendees) and asks Claude Haiku via the Ubuntu LiteLLM proxy whether
they're the same person. Persists verdict/confidence/reason on the row.

Does NOT auto-merge — the contacts_viewer /review page renders the LLM
verdict alongside the heuristic so the human stays in the loop. Idempotent
across reruns: rows with llm_verdict already set are skipped unless
--rerun is passed.

Usage:
    python3 entity_resolver_agent.py                      # default 50 rows
    python3 entity_resolver_agent.py --limit 1000         # full sweep
    python3 entity_resolver_agent.py --rerun --limit 5    # re-process top 5
    python3 entity_resolver_agent.py --dry-run --limit 3  # preview prompts
"""

import argparse
import datetime
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LITELLM_URL = "http://100.121.184.27:4000/v1/chat/completions"
LITELLM_MODEL = "claude-haiku-4-5"
DB_PATH = str(Path.home() / "contacts.db")
DEFAULT_LIMIT = 50
TIMEOUT_SEC = 60

SYSTEM_PROMPT = """You are deduplicating people in a personal professional contacts database. \
Given two name variants and their meeting context, decide if they refer to the same person. \
Reply with ONLY a JSON object — no prose, no code fences.

Schema:
{"verdict": "merge" | "distinct" | "ambiguous", "confidence": 0.0-1.0, "reason": "one sentence (max 140 chars)"}

Decision guide:
- "merge": you are confident they are the same person (e.g. one is a clear nickname/first-name of the other AND their orgs and co-attendees match)
- "distinct": you are confident they are different people (e.g. same first name but different primary orgs, no co-meeting overlap, distinct topic profiles)
- "ambiguous": evidence is mixed or thin — when in doubt, return ambiguous, not merge

Strong merge signals: same primary org, overlapping co-attendees, overlapping recent meeting topics, one name is a strict prefix of the other.
Strong distinct signals: different primary orgs with no overlap, different topical worlds, no shared co-attendees.
"""


def call_haiku(messages):
    payload = json.dumps({
        "model": LITELLM_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 200,
    }).encode()
    req = urllib.request.Request(
        LITELLM_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"]


def parse_verdict(content):
    """Extract verdict JSON from a Haiku response, even if it has prose around it."""
    m = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if obj.get("verdict") not in ("merge", "distinct", "ambiguous"):
        return None
    try:
        obj["confidence"] = max(0.0, min(1.0, float(obj.get("confidence", 0))))
    except (TypeError, ValueError):
        obj["confidence"] = 0.0
    obj["reason"] = (obj.get("reason") or "")[:200]
    return obj


def ensure_schema(conn):
    """Add llm_* columns to merge_suggestions if they don't exist."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(merge_suggestions)")}
    additions = [
        ("llm_verdict", "TEXT"),
        ("llm_confidence", "REAL"),
        ("llm_reason", "TEXT"),
        ("llm_processed_at", "TEXT"),
    ]
    for name, type_ in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE merge_suggestions ADD COLUMN {name} {type_}")
    conn.commit()


def gather_context(conn, raw_name):
    """Build a compact context block for one name. Pulls from attendees/meetings."""
    rows = conn.execute(
        """
        SELECT m.filename, m.category, m.topic, m.date
        FROM attendees a JOIN meetings m ON m.id = a.meeting_id
        WHERE a.person_name = ?
        ORDER BY m.date DESC
        """,
        (raw_name,),
    ).fetchall()
    if not rows:
        return {"meetings": 0, "categories": [], "recent": [], "co_attendees": []}

    cats = {}
    for r in rows:
        cats[r["category"] or "?"] = cats.get(r["category"] or "?", 0) + 1
    top_cats = sorted(cats.items(), key=lambda x: -x[1])[:3]

    recent = [(r["date"], r["category"] or "?", r["topic"] or "") for r in rows[:5]]

    meeting_ids = [r["filename"] for r in rows[:30]]
    co = {}
    if meeting_ids:
        placeholders = ",".join("?" * len(meeting_ids))
        co_rows = conn.execute(
            f"""
            SELECT a.person_name, COUNT(*) as n
            FROM attendees a JOIN meetings m ON m.id = a.meeting_id
            WHERE m.filename IN ({placeholders}) AND a.person_name != ?
            GROUP BY a.person_name ORDER BY n DESC LIMIT 8
            """,
            meeting_ids + [raw_name],
        ).fetchall()
        co = [(r["person_name"], r["n"]) for r in co_rows]

    return {
        "meetings": len(rows),
        "categories": top_cats,
        "recent": recent,
        "co_attendees": co,
    }


def format_block(label, name, org, ctx):
    lines = [f"Person {label}: \"{name}\""]
    if org:
        lines.append(f"  Primary org: {org}")
    lines.append(f"  Total meetings: {ctx['meetings']}")
    if ctx["categories"]:
        lines.append("  Top categories: " + ", ".join(f"{c} ({n})" for c, n in ctx["categories"]))
    if ctx["recent"]:
        lines.append("  Recent meetings:")
        for date, cat, topic in ctx["recent"]:
            lines.append(f"    - {date} {cat}: {topic[:80]}")
    if ctx["co_attendees"]:
        lines.append("  Co-attendees: " + ", ".join(f"{n} ({c}x)" for n, c in ctx["co_attendees"]))
    return "\n".join(lines)


def build_user_prompt(s, ctx_a, ctx_b):
    blocks = [
        format_block("A", s["canonical_name"], s["canonical_org"], ctx_a),
        "",
        format_block("B", s["alias_name"], s["alias_org"], ctx_b),
        "",
        f"Heuristic reason: {s['reason']} (heuristic confidence {s['confidence']:.2f})",
    ]
    return "Are these the same person?\n\n" + "\n".join(blocks)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"Max suggestions to process this run (default {DEFAULT_LIMIT})")
    p.add_argument("--rerun", action="store_true",
                   help="Re-process rows that already have llm_verdict set")
    p.add_argument("--dry-run", action="store_true",
                   help="Print prompts and skip the LLM call")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout pauses on transient locks instead of erroring out.
    # WAL is preferred (concurrent reads while we write) but can't be set
    # if another process is already holding the journal — best effort only.
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # already locked by another reader; rollback journal + busy_timeout still works
    ensure_schema(conn)

    where = "status = 'pending'"
    if not args.rerun:
        where += " AND llm_verdict IS NULL"
    rows = conn.execute(
        f"SELECT * FROM merge_suggestions WHERE {where} "
        "ORDER BY confidence DESC, id ASC LIMIT ?",
        (args.limit,),
    ).fetchall()

    print(f"entity_resolver_agent: {len(rows)} suggestion(s) to process "
          f"(limit={args.limit}, rerun={args.rerun}, dry_run={args.dry_run})")
    if not rows:
        return 0

    processed = errors = 0
    counts = {"merge": 0, "distinct": 0, "ambiguous": 0}

    for s in rows:
        ctx_a = gather_context(conn, s["canonical_raw"])
        ctx_b = gather_context(conn, s["alias_raw"])
        user_prompt = build_user_prompt(s, ctx_a, ctx_b)

        if args.dry_run:
            print(f"\n--- #{s['id']} {s['canonical_name']!r} vs {s['alias_name']!r} ---")
            print(user_prompt)
            continue

        v = None
        last_err = None
        for attempt in (1, 2):
            try:
                content = call_haiku([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ])
                v = parse_verdict(content)
                if not v:
                    raise ValueError(f"could not parse verdict from: {content[:200]!r}")
                break
            except (OSError, urllib.error.URLError, ValueError, TimeoutError, socket.timeout) as e:
                last_err = e
                if attempt == 1:
                    time.sleep(3)
        if v is None:
            errors += 1
            print(f"  #{s['id']} ERROR after retry: {last_err}", file=sys.stderr)
            continue

        conn.execute(
            "UPDATE merge_suggestions SET llm_verdict=?, llm_confidence=?, "
            "llm_reason=?, llm_processed_at=? WHERE id=?",
            (v["verdict"], v["confidence"], v["reason"],
             datetime.datetime.now().isoformat(timespec="seconds"), s["id"]),
        )
        conn.commit()
        counts[v["verdict"]] += 1
        processed += 1
        print(f"  #{s['id']} {s['canonical_name']!r} ↔ {s['alias_name']!r}: "
              f"{v['verdict']} ({v['confidence']:.2f}) — {v['reason']}")

    print(f"\nDone. processed={processed} errors={errors}  "
          f"merge={counts['merge']} distinct={counts['distinct']} ambiguous={counts['ambiguous']}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
