"""
Project tagger for action items and decisions.

Resolves action item owners to their briefing-level project/client.
Used by build_graph.py to populate the `project` column.

Key design decisions:
- ADAPT people are tagged as DCC (they're embedded at DCC through ADAPT)
- Uses PERSON_CATEGORY + CATEGORY_NAME_EXPANSIONS for resolution
- Short names ("Chris", "Kizzer") resolve via name expansions
- Eoin Lane returns empty string (works across all clients)
- Unknown owners return empty string (fall back to meeting category)
"""

from shared.config import PERSON_CATEGORY
from shared.name_expansions import CATEGORY_NAME_EXPANSIONS

# For briefing purposes, ADAPT people work on DCC projects
BRIEFING_OVERRIDES = {
    "ADAPT": "DCC",
}


def build_owner_project_tagger():
    """Build a lowercase-owner → briefing-category mapping.

    Returns dict: {"christopher kelly": "DCC", "kizzer": "DCC", ...}
    """
    tagger = {}

    def _briefing_cat(cat):
        return BRIEFING_OVERRIDES.get(cat, cat)

    # 1. Direct PERSON_CATEGORY entries
    for full_name, cat in PERSON_CATEGORY.items():
        bcat = _briefing_cat(cat)
        tagger[full_name.lower()] = bcat
        # Also add first name if unambiguous
        first = full_name.split()[0].lower()
        if first not in tagger:
            tagger[first] = bcat
        else:
            # First name is ambiguous across categories — remove it
            if tagger[first] != bcat:
                tagger[first] = ""  # ambiguous, will be filtered out

    # 2. Name expansions (short names / mishearings)
    for cat, names in CATEGORY_NAME_EXPANSIONS.items():
        for short, full in names.items():
            bcat_full = PERSON_CATEGORY.get(full, "")
            if bcat_full:
                bcat = _briefing_cat(bcat_full)
            else:
                bcat = _briefing_cat(cat)
            tagger[short.lower()] = bcat

    # 3. Common Eoin aliases — return empty (cross-client)
    for alias in ("eoin lane", "owen lane", "owen", "eoin"):
        tagger[alias] = ""

    # Remove ambiguous entries (empty string means "don't know")
    return {k: v for k, v in tagger.items() if v}
