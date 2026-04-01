"""
entity_resolution.py — Find candidate duplicate people in contacts.db.
Called from build_contacts_db.py after the DB is built.
Stores suggestions in merge_suggestions table for review in the web UI.
"""

import re
import sqlite3
from difflib import SequenceMatcher


# ── string helpers ────────────────────────────────────────────────────────────

def normalise(name):
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", name.lower())).strip()


def first_word(name):
    return normalise(name).split()[0] if name.strip() else ""


def edit_distance(a, b):
    """Standard Levenshtein distance."""
    a, b = a.lower(), b.lower()
    if a == b:
        return 0
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            prev, dp[j] = dp[j], prev if a[i-1] == b[j-1] else 1 + min(prev, dp[j], dp[j-1])
    return dp[n]


def name_similarity(a, b):
    return SequenceMatcher(None, normalise(a), normalise(b)).ratio()


# ── detection ─────────────────────────────────────────────────────────────────

def _meeting_set(c, raw_name):
    return set(
        r[0] for r in c.execute(
            "SELECT m.filename FROM attendees a "
            "JOIN meetings m ON m.id = a.meeting_id "
            "WHERE a.person_name = ?", (raw_name,)
        ).fetchall()
    )


def detect_reason(n1, n2):
    """
    Returns (reason_code, base_confidence) or (None, 0) if no match.
    n1, n2 are display names (resolved if available).
    """
    if normalise(n1) == normalise(n2):
        return None, 0.0  # identical display names — same resolution, not a dup

    na, nb = normalise(n1), normalise(n2)
    wa, wb = na.split(), nb.split()

    # 1. One name is a single-word first name, the other is "FirstName Surname"
    #    and the first words match exactly
    if wa[0] == wb[0]:
        if len(wa) == 1 and len(wb) > 1:
            return "first_name_only", 0.75
        if len(wb) == 1 and len(wa) > 1:
            return "first_name_only", 0.75

    # 2. One full name is contained in the other at a word boundary
    #    e.g. "Jeremy" in "Jeremy Ryan" — but NOT "Rich" inside "Richard Kelly"
    if len(wa) <= len(wb) and wb[:len(wa)] == wa:
        return "name_contained", 0.80
    if len(wb) <= len(wa) and wa[:len(wb)] == wb:
        return "name_contained", 0.80

    # 3. Edit distance ≤ 2 on the full normalised name (catches typos: Floyd/Flood, Hell/Howell)
    #    Only apply when names are of similar length (avoid Rich/Richard Kelly)
    if abs(len(na) - len(nb)) <= 3:
        dist = edit_distance(na, nb)
        if dist <= 2 and max(len(na), len(nb)) >= 4:
            return f"edit_distance_{dist}", 0.65

    # 4. High sequence similarity on short single-word names
    if len(wa) == 1 and len(wb) == 1:
        sim = name_similarity(n1, n2)
        if sim >= 0.75:
            return f"similar_{int(sim*100)}pct", 0.55

    return None, 0.0


def score_pair(c, p1, p2):
    """
    Score a candidate pair. Returns None if they should be skipped.
    p1, p2 are sqlite3.Row objects from the people table.
    """
    n1 = p1["display_name"]
    n2 = p2["display_name"]

    reason, base_conf = detect_reason(n1, n2)
    if not reason:
        return None

    # Get meeting sets using RAW names (as stored in attendees)
    m1 = _meeting_set(c, p1["name"])
    m2 = _meeting_set(c, p2["name"])

    # If they ever appear in the same meeting → definitely different people
    if m1 & m2:
        return None

    confidence = base_conf

    # Boost if same primary org
    if p1["primary_org"] and p2["primary_org"] and p1["primary_org"] == p2["primary_org"]:
        confidence += 0.12

    # Boost based on meeting subset ratio
    # (how much of the smaller person's meetings are a subset of the larger's KB)
    # Since they never co-occur, we check if meetings tend to cluster together in time
    if m1 and m2:
        smaller = min(m1, m2, key=len)
        larger  = max(m1, m2, key=len)
        # Check if they share common "neighbours" — meetings on the same day or with same people
        subset_ratio = len(smaller & larger) / len(smaller)  # 0 since disjoint
        # Use total meeting count ratio instead: if one has far more meetings, likely the full name
        count_ratio = min(len(m1), len(m2)) / max(len(m1), len(m2)) if max(len(m1), len(m2)) else 0
        confidence += count_ratio * 0.08

    # Penalise if orgs differ meaningfully
    if (p1["primary_org"] and p2["primary_org"]
            and p1["primary_org"] != p2["primary_org"]
            and "other" not in p1["primary_org"]
            and "other" not in p2["primary_org"]):
        confidence -= 0.20

    if confidence < 0.35:
        return None

    # Canonical = the one with more meetings (or the longer/fuller name)
    if p1["meeting_count"] >= p2["meeting_count"]:
        canonical, alias = p1, p2
    else:
        canonical, alias = p2, p1

    return {
        "canonical_raw":   canonical["name"],
        "canonical_name":  canonical["display_name"],
        "canonical_org":   canonical["primary_org"] or "",
        "canonical_count": canonical["meeting_count"],
        "alias_raw":       alias["name"],
        "alias_name":      alias["display_name"],
        "alias_org":       alias["primary_org"] or "",
        "alias_count":     alias["meeting_count"],
        "reason":          reason,
        "confidence":      round(confidence, 3),
    }


# ── main entry point ──────────────────────────────────────────────────────────

def build_suggestions(conn):
    """Populate merge_suggestions table. Called after DB build."""

    # Fetch all people with display names
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS merge_suggestions (
            id             INTEGER PRIMARY KEY,
            canonical_raw  TEXT,
            canonical_name TEXT,
            canonical_org  TEXT,
            canonical_count INTEGER,
            alias_raw      TEXT,
            alias_name     TEXT,
            alias_org      TEXT,
            alias_count    INTEGER,
            reason         TEXT,
            confidence     REAL,
            status         TEXT DEFAULT 'pending',
            UNIQUE(canonical_raw, alias_raw)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS dismissed_pairs (
            name1 TEXT, name2 TEXT,
            PRIMARY KEY (name1, name2)
        )
    """)

    dismissed = set(
        (r[0], r[1]) for r in c.execute("SELECT name1, name2 FROM dismissed_pairs").fetchall()
    )

    people = c.execute("""
        SELECT name, COALESCE(resolved_name, name) AS display_name,
               primary_org, meeting_count
        FROM people
        ORDER BY meeting_count DESC
    """).fetchall()

    new_suggestions = 0

    for i, p1 in enumerate(people):
        for p2 in people[i + 1:]:
            pair = (p1["name"], p2["name"])
            pair_r = (p2["name"], p1["name"])
            if pair in dismissed or pair_r in dismissed:
                continue

            result = score_pair(c, p1, p2)
            if not result:
                continue

            # Upsert — don't overwrite if already reviewed
            existing = c.execute(
                "SELECT status FROM merge_suggestions "
                "WHERE (canonical_raw=? AND alias_raw=?) OR (canonical_raw=? AND alias_raw=?)",
                (result["canonical_raw"], result["alias_raw"],
                 result["alias_raw"], result["canonical_raw"])
            ).fetchone()

            if existing and existing["status"] != "pending":
                continue  # already acted on

            if not existing:
                c.execute("""
                    INSERT OR IGNORE INTO merge_suggestions
                    (canonical_raw, canonical_name, canonical_org, canonical_count,
                     alias_raw, alias_name, alias_org, alias_count, reason, confidence)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (result["canonical_raw"], result["canonical_name"],
                      result["canonical_org"], result["canonical_count"],
                      result["alias_raw"], result["alias_name"],
                      result["alias_org"], result["alias_count"],
                      result["reason"], result["confidence"]))
                new_suggestions += 1

    conn.commit()

    total = c.execute("SELECT COUNT(*) FROM merge_suggestions WHERE status='pending'").fetchone()[0]
    if new_suggestions or total:
        print(f"  {new_suggestions} new merge suggestions ({total} pending review)")
