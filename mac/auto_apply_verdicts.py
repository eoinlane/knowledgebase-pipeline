#!/usr/bin/env python3
"""auto_apply_verdicts.py

Drains the entity-resolver backlog by auto-applying high-confidence LLM
verdicts. The LLM judgment in entity_resolver_agent.py writes verdicts
into merge_suggestions; before this script, those verdicts sat unread
waiting on a human at /review. With ~1k pending and only ~36 actual
merge candidates, the human review never happened and the noise grew.

Defaults:
  - merge: llm_verdict='merge' AND llm_confidence >= 0.85 AND passes
    safety guards (see is_safe_merge — only handles unambiguous patterns:
    email-prefix dot form, SPEAKER labels, close spelling variants).
  - dismiss: llm_verdict='distinct' AND llm_confidence >= 0.90
  - ambiguous / lower confidence / unsafe-pattern merges: left pending
    for human review.

Safety guards on merge — only the unambiguous patterns are auto-applied:
The contacts DB has 5 Alans, 9 Neils, 6 Conors, 2 Shakespeares — first-name
→ full-name merges are fundamentally ambiguous and routinely wrong. We
hand those to /review. Auto-merge restricted to:
  (a) email-prefix form: one side matches '^[a-z]+\\.[a-z]+$'
  (b) SPEAKER_NN ↔ Speaker_NN (transcript labels)
  (c) spelling variant: both multi-word, share a token, edit distance ≤ 2

Merge logic mirrors contacts_viewer.api_merge:
  1. Reassign attendees rows from alias → canonical_raw (with conflict
     cleanup against duplicate rows on the same meeting).
  2. Set alias's resolved_name / resolved_slug to canonical's display.
  3. Recompute canonical's meeting_count.
  4. Write {alias_raw: {name: canonical_display}} into kb_corrections.json
     so the rebuild propagates it through the nightly pipeline.
  5. Mark suggestion status='merged'.

Dismiss logic mirrors contacts_viewer.api_dismiss:
  1. INSERT INTO dismissed_pairs both directions (kills future re-detection).
  2. Mark suggestion status='dismissed'.

apply_kb_corrections.py is run ONCE at the end if any merges happened.

Usage:
  python3 auto_apply_verdicts.py --dry-run        # preview, no changes
  python3 auto_apply_verdicts.py --apply          # do it
  python3 auto_apply_verdicts.py --apply --merge-conf 0.92 --distinct-conf 0.95
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

CONTACTS_DB = Path.home() / "contacts.db"
CORRECTIONS_FILE = Path.home() / "kb_corrections.json"
APPLY_SCRIPT = Path.home() / "knowledgebase-pipeline" / "mac" / "apply_kb_corrections.py"


def load_corrections():
    if not CORRECTIONS_FILE.exists():
        return {"people": {}, "meetings": {}}
    with open(CORRECTIONS_FILE) as f:
        return json.load(f)


def save_corrections(data):
    with open(CORRECTIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def slugify(name):
    return re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))


_EMAIL_PREFIX_RE = re.compile(r"^[a-z]+\.[a-z]+$")
_SPEAKER_RE = re.compile(r"^speaker_\d+$", re.IGNORECASE)


def _edit_distance(a, b):
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 3:
        return 99
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(curr[j-1] + 1, prev[j] + 1,
                          prev[j-1] + (0 if ca == cb else 1))
        prev = curr
    return prev[-1]


def is_safe_merge(canonical_raw, alias_raw):
    """Only auto-merge unambiguous identity patterns; punt the rest to /review.

    Returns (is_safe: bool, reason: str, canonical_raw: str, alias_raw: str).
    The returned canonical/alias may be swapped from the input to ensure the
    human-readable form wins (e.g. when entity_resolution picked an
    email-prefix as canonical because it had more meeting hits)."""
    c, a = canonical_raw.strip(), alias_raw.strip()
    c_low, a_low = c.lower(), a.lower()

    # (a) Email-prefix dot form on one side AND the other side's name,
    # stripped to letters, contains BOTH prefix parts. The "contains both"
    # check is what stops 'tom.pollock' from getting merged into 'Tom
    # Curran' just because the LLM ran with shared first name + org.
    # Direction: human-readable side wins canonical regardless of how
    # entity_resolution ordered them.
    def _other_contains_both(email_side, other_side):
        parts = email_side.split(".")
        if len(parts) != 2:
            return False
        clean_other = re.sub(r"[^a-z]", "", other_side.lower())
        return all(p in clean_other for p in parts)
    # The "human-readable" side must start with a letter — guards against
    # making things like ': Stephen Rigney' the canonical display name.
    if (_EMAIL_PREFIX_RE.match(a_low) and _other_contains_both(a_low, c_low)
            and c[:1].isalpha()):
        return True, "email-prefix dedupe", c, a
    if (_EMAIL_PREFIX_RE.match(c_low) and _other_contains_both(c_low, a_low)
            and a[:1].isalpha()):
        return True, "email-prefix dedupe", a, c

    # (b) SPEAKER_NN ↔ Speaker_NN — transcript labels with no real name.
    if _SPEAKER_RE.match(c) and _SPEAKER_RE.match(a):
        return True, "speaker-label dedupe", c, a

    # (c) Both multi-word, share at least one identical token, edit
    # distance ≤ 2 on the full string. Catches "Connor Daly" vs "Conor Daly".
    c_tokens = set(c_low.split())
    a_tokens = set(a_low.split())
    if (len(c_tokens) >= 2 and len(a_tokens) >= 2
            and c_tokens & a_tokens
            and _edit_distance(c_low, a_low) <= 2):
        return True, "spelling variant", c, a

    return False, "ambiguous (first-name collision likely) — human review", c, a


def merge_one(conn, canonical_raw, alias_raw, sid):
    """Mirror of contacts_viewer.api_merge SQL. Returns canonical display name."""
    row = conn.execute(
        "SELECT COALESCE(resolved_name, name) AS display FROM people WHERE name=?",
        (canonical_raw,),
    ).fetchone()
    canonical_display = row[0] if row else canonical_raw

    # Conflict cleanup: drop alias rows from meetings where canonical already attends
    conn.execute(
        "DELETE FROM attendees WHERE person_name = ? "
        "AND meeting_id IN (SELECT meeting_id FROM attendees WHERE person_name = ?)",
        (alias_raw, canonical_raw),
    )
    conn.execute(
        "UPDATE attendees SET person_name=? WHERE person_name=?",
        (canonical_raw, alias_raw),
    )
    conn.execute(
        "UPDATE people SET resolved_name=?, resolved_slug=? WHERE name=?",
        (canonical_display, slugify(canonical_display), alias_raw),
    )
    total = conn.execute(
        "SELECT COUNT(DISTINCT meeting_id) FROM attendees WHERE person_name=?",
        (canonical_raw,),
    ).fetchone()[0]
    conn.execute("UPDATE people SET meeting_count=? WHERE name=?", (total, canonical_raw))
    conn.execute("UPDATE merge_suggestions SET status='merged' WHERE id=?", (sid,))
    return canonical_display


def dismiss_one(conn, n1, n2, sid):
    conn.execute(
        "INSERT OR IGNORE INTO dismissed_pairs (name1, name2) VALUES (?,?)", (n1, n2)
    )
    conn.execute(
        "INSERT OR IGNORE INTO dismissed_pairs (name1, name2) VALUES (?,?)", (n2, n1)
    )
    conn.execute("UPDATE merge_suggestions SET status='dismissed' WHERE id=?", (sid,))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually do it. Without this flag, runs as dry-run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview without changes (default behaviour).")
    ap.add_argument("--merge-conf", type=float, default=0.85,
                    help="Min LLM confidence to auto-merge (default 0.85)")
    ap.add_argument("--distinct-conf", type=float, default=0.90,
                    help="Min LLM confidence to auto-dismiss (default 0.90)")
    args = ap.parse_args()

    apply = args.apply and not args.dry_run

    conn = sqlite3.connect(CONTACTS_DB)

    merge_rows = conn.execute(
        """SELECT id, canonical_raw, alias_raw, canonical_name, alias_name,
                  llm_confidence, llm_reason
           FROM merge_suggestions
           WHERE status='pending' AND llm_verdict='merge' AND llm_confidence >= ?
           ORDER BY llm_confidence DESC, id ASC""",
        (args.merge_conf,),
    ).fetchall()
    merges, skipped = [], []
    for r in merge_rows:
        ok, why, c_eff, a_eff = is_safe_merge(r[1], r[2])
        # Stash effective (post-swap) canonical/alias on the tuple.
        (merges if ok else skipped).append((r, why, c_eff, a_eff))

    dismisses = conn.execute(
        """SELECT id, canonical_raw, alias_raw, canonical_name, alias_name,
                  llm_confidence
           FROM merge_suggestions
           WHERE status='pending' AND llm_verdict='distinct' AND llm_confidence >= ?
           ORDER BY llm_confidence DESC, id ASC""",
        (args.distinct_conf,),
    ).fetchall()

    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Thresholds: merge>={args.merge_conf}  distinct>={args.distinct_conf}")
    print()
    print(f"Auto-merge candidates (passed safety guards): {len(merges)}")
    for (sid, _c, _a, _cn, _an, conf, _reason), why, c_eff, a_eff in merges:
        print(f"  [{conf:.2f}] #{sid}  {a_eff!r} → {c_eff!r}  ({why})")
    print()
    print(f"Skipped (unsafe pattern — left for human /review): {len(skipped)}")
    for (sid, c_raw, a_raw, _cn, _an, conf, _reason), why, _c, _a in skipped[:8]:
        print(f"  [{conf:.2f}] #{sid}  {a_raw!r} → {c_raw!r}  ({why})")
    if len(skipped) > 8:
        print(f"  ... and {len(skipped) - 8} more")
    print()
    print(f"Auto-dismiss candidates: {len(dismisses)}")
    for sid, c_raw, a_raw, c_name, a_name, conf in dismisses[:5]:
        print(f"  [{conf:.2f}] #{sid}  {c_raw!r} ≠ {a_raw!r}")
    if len(dismisses) > 5:
        print(f"  ... and {len(dismisses) - 5} more")

    if not apply:
        conn.close()
        print("\nDry-run only. Pass --apply to commit.")
        return 0

    corrections = load_corrections()
    merged_count = 0
    for (sid, _c, _a, _cn, _an, _conf, _r), _why, c_eff, a_eff in merges:
        display = merge_one(conn, c_eff, a_eff, sid)
        entry = corrections["people"].setdefault(a_eff, {})
        entry["name"] = display
        merged_count += 1

    dismissed_count = 0
    for sid, c_raw, a_raw, _, _, _ in dismisses:
        dismiss_one(conn, c_raw, a_raw, sid)
        dismissed_count += 1

    conn.commit()
    conn.close()

    if merged_count > 0:
        save_corrections(corrections)
        print(f"\nApplied {merged_count} merge(s), {dismissed_count} dismissal(s).")
        print("Running apply_kb_corrections.py to patch markdown files...")
        r = subprocess.run(
            ["/usr/local/bin/python3", str(APPLY_SCRIPT)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"apply_kb_corrections.py failed: {r.stderr[:300]}", file=sys.stderr)
            return 4
        # Show only the summary lines, not every patched file
        for line in r.stdout.splitlines()[-10:]:
            print(f"  {line}")
    else:
        print(f"\nApplied 0 merge(s), {dismissed_count} dismissal(s). No markdown patch needed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
