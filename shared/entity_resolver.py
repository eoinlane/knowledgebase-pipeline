"""
Unified entity resolver for the knowledgebase pipeline.

Single source of truth for name resolution. All scripts import resolve() from here.

Priority order:
1. Hardcoded mishearings (confirmed WhisperX errors)
2. KB corrections (manual name overrides from ~/kb_corrections.json)
3. Name expansions (per-category short-name mappings from name_expansions.py)
4. Safe contacts.db merges (unambiguous first-name expansions + spelling variants)

Usage:
    from shared.entity_resolver import build_resolver, resolve_name

    resolver = build_resolver()           # build once at startup
    canonical = resolve_name("Cathal Murphy", resolver)  # → "Cathal Bellew"
    slug = resolve_slug("cathal-murphy", resolver)       # → "cathal-bellew"
"""

import json
import os
import re
import sqlite3

# ── Hardcoded mishearings (confirmed WhisperX errors) ─────────────────────────
MISHEARINGS = {
    "pat-nester": "pat-nestor",
    "owen-lane": "eoin-lane",
    "owen-lane-(eoin)": "eoin-lane",
    "owen-lane-(eoin-lane)": "eoin-lane",
    "owen-(eoin-lane)": "eoin-lane",
    "eoghan-lane": "eoin-lane",
    "declan-sheen": "declan-sheehan",
    "aidan-bly": "aidan-blighe",
    "david-floods": "david-flood",
    "david-floyd": "david-flood",
    "ian-o'keefe": "ian-o'keeffe",
    "philip-lestrange": "philip-l'estrange",
    "rob-hell": "rob-howell",
    "hugh-cregan": "hugh-creegan",
    "kevin-dunn": "kevin-dunne",
    "cathal-murphy": "cathal-bellew",
}

# ── Junk patterns (not person names) ─────────────────────────────────────────
NON_PERSONS = {"surveillance-authority", "new-ceo", "microsoft-contacts",
               "microsoft-representatives", "dr--jamie", "father"}

JUNK_PATTERNS = ["team", "officer", "authority", "grandfather", "father",
                 "vp-of-", "chief-", "participants", "resource-placed", "'s-"]

CONTACTS_DB = os.path.expanduser("~/contacts.db")
CORRECTIONS_FILE = os.path.expanduser("~/kb_corrections.json")


def slugify(name):
    return re.sub(r"[\s_-]+", "-", name.lower()).strip("-")


def _load_name_expansions():
    """Load per-category name expansions, flatten to slug mappings."""
    mappings = {}
    try:
        from shared.name_expansions import CATEGORY_NAME_EXPANSIONS
        for cat, table in CATEGORY_NAME_EXPANSIONS.items():
            for short, full in table.items():
                s_short = slugify(short)
                s_full = slugify(full)
                if s_short != s_full:
                    mappings[s_short] = s_full
    except ImportError:
        pass
    return mappings


def _load_kb_corrections():
    """Load manual name corrections from kb_corrections.json."""
    mappings = {}
    if os.path.exists(CORRECTIONS_FILE):
        try:
            with open(CORRECTIONS_FILE) as f:
                corr = json.load(f)
            for raw, data in corr.get("people", {}).items():
                canonical = data.get("name", raw)
                if canonical != raw:
                    mappings[slugify(raw)] = slugify(canonical)
        except (json.JSONDecodeError, OSError):
            pass
    return mappings


def _load_safe_contacts():
    """Load safe auto-merges from contacts.db resolved_name.

    Only applies:
    - First-name-only → full name when unambiguous (no other people with same first name)
    - Spelling variants (same first name, surname edit distance ≤ 2)
    - Email → name
    """
    mappings = {}
    if not os.path.exists(CONTACTS_DB):
        return mappings

    conn = sqlite3.connect(CONTACTS_DB)
    rows = conn.execute("""
        SELECT name, resolved_name FROM people
        WHERE resolved_name IS NOT NULL
          AND resolved_name != name
          AND name NOT LIKE 'SPEAKER%'
    """).fetchall()

    for name, resolved in rows:
        parts = name.strip().split()
        rparts = resolved.strip().split()
        s_name = slugify(name)
        s_resolved = slugify(resolved)

        if s_name == s_resolved:
            continue

        # Email → name: always safe
        if "@" in name:
            mappings[s_name] = s_resolved
            continue

        # First-name-only → full name: safe if unambiguous
        if len(parts) == 1 and len(rparts) >= 2:
            n = conn.execute("""
                SELECT COUNT(DISTINCT COALESCE(resolved_name, name))
                FROM people
                WHERE (name = ? OR name LIKE ? || ' %' OR resolved_name LIKE ? || ' %')
                  AND name NOT LIKE 'SPEAKER%'
                  AND COALESCE(resolved_name, name) LIKE '% %'
            """, (name, name, name)).fetchone()[0]
            if n == 1:
                mappings[s_name] = s_resolved
            continue

        # Both multi-word: same first name + similar surname
        if len(parts) >= 2 and len(rparts) >= 2:
            if parts[0].lower() == rparts[0].lower():
                s1, s2 = parts[-1].lower(), rparts[-1].lower()
                dist = sum(1 for a, b in zip(s1, s2) if a != b) + abs(len(s1) - len(s2))
                if dist <= 2:
                    # Prefer the longer (fuller) name
                    if len(s_name) > len(s_resolved):
                        mappings[s_resolved] = s_name
                    else:
                        mappings[s_name] = s_resolved

    conn.close()
    return mappings


def build_resolver():
    """Build the unified resolver. Call once at startup.

    Returns a dict: slug → canonical_slug
    Priority: hardcoded > corrections > expansions > contacts
    """
    resolver = {}
    resolver.update(_load_safe_contacts())   # lowest priority
    resolver.update(_load_name_expansions())
    resolver.update(_load_kb_corrections())
    resolver.update(MISHEARINGS)             # highest priority
    return resolver


def resolve_slug(slug, resolver):
    """Resolve a person slug to its canonical form. Returns None to discard junk."""
    # Strip question marks and normalise dots
    slug = slug.rstrip("?")
    slug = slug.replace(".", "-")

    # Discard junk
    if not slug or len(slug) < 3:
        return None
    if slug.startswith("speaker-") or slug == "unknown" or slug.startswith("unknown-"):
        return None
    if "-and-" in slug or slug.endswith("-and-team"):
        return None
    if ",-" in slug:
        return None
    if "-(" in slug:
        return None
    if ";" in slug or "-with-" in slug:
        return None
    if "-to-" in slug and len(slug) > 30:
        return None
    if "-or-" in slug:
        return None
    if "@" in slug:
        return None
    if slug in NON_PERSONS:
        return None
    if any(p in slug for p in JUNK_PATTERNS):
        return None

    # Apply resolver
    return resolver.get(slug, slug)


def resolve_name(name, resolver):
    """Resolve a display name to its canonical form."""
    slug = slugify(name)
    resolved_slug = resolve_slug(slug, resolver)
    if resolved_slug is None:
        return None
    if resolved_slug == slug:
        return name  # no change
    # Convert slug back to title case
    return resolved_slug.replace("-", " ").title()
