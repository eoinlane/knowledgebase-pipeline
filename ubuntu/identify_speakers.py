#!/usr/bin/env python3
"""
Identify speakers in a WhisperX transcript using qwen2.5:14b via ollama-box.
Reads key_people from the classification CSV, asks the LLM to map
SPEAKER_XX labels to real names, then rewrites the transcript in-place.

Usage: python3 identify_speakers.py <transcript_txt> <csv_path>

High-confidence guesses → [Name]
Low/medium-confidence   → [Name?]
Unknown                 → left as [SPEAKER_XX]
"""

import sys, os, json, re, csv
from collections import defaultdict
import urllib.request
import numpy as np

# Import shared config (with fallback for standalone use)
PIPELINE_DIR = os.environ.get("PIPELINE_DIR", os.path.expanduser("~/knowledgebase-pipeline"))
if os.path.isdir(PIPELINE_DIR) and PIPELINE_DIR not in sys.path:
    sys.path.insert(0, PIPELINE_DIR)
try:
    from shared.config import OLLAMA_URL, MODEL
    from shared.name_expansions import CATEGORY_NAME_EXPANSIONS
    _SHARED_LOADED = True
except ImportError:
    _SHARED_LOADED = False
    OLLAMA_URL = "http://192.168.0.70:11434/api/chat"
    MODEL = "qwen2.5:14b"
MAPPINGS_FILE    = os.path.expanduser("~/speaker_mappings.json")
REGISTRY_FILE    = os.path.expanduser("~/speaker_registry.json")
CATALOG_FILE     = os.path.expanduser("~/voice_catalog.json")
EMBEDDINGS_DIR   = os.path.expanduser("~/audio-inbox/Embeddings")
KB_MEETINGS_DIR  = os.path.expanduser("~/knowledge_base/meetings")

# Voice match thresholds — tuned 2026-04-28 after misattribution incident
# (Cathal Bellew's 1-sample fingerprint matched Declan Sheehan's 1-sample
# fingerprint at 0.724, mistakenly clearing the medium bar).
#
# Two changes from the original 0.80/0.70 single-threshold approach:
#   1. Match score = mean of TOP 3 sample similarities (not mean over all).
#      Preserves variation, robust to a single bad sample.
#   2. Margin requirement: best score must beat runner-up by ≥ MARGIN.
#      Sub-margin matches fall through to LLM identification instead of
#      being silently labelled with the wrong name.
VOICE_THRESHOLD_HIGH   = 0.85  # was 0.80
VOICE_THRESHOLD_MEDIUM = 0.75  # was 0.70
VOICE_MARGIN_HIGH      = 0.05
VOICE_MARGIN_MEDIUM    = 0.03

# Auto-enrol unambiguous matches: if a voice match is very high AND
# significantly beats the runner-up, append this recording's embedding to
# that person's catalog (rolling 20). Grows the catalog without needing
# manual review_speakers.py confirmations.
AUTO_ENROL_MIN_SIM    = 0.92
AUTO_ENROL_MIN_MARGIN = 0.10

# Category-specific name expansion — imported from shared, with inline fallback
if not _SHARED_LOADED:
    CATEGORY_NAME_EXPANSIONS = {
    "DCC": {
        "chris": "Christopher Kelly",
        "christopher": "Christopher Kelly",
        "kizzer": "Khizer Ahmed Biyabani",
        "khizer": "Khizer Ahmed Biyabani",
        "kizer": "Khizer Ahmed Biyabani",
        "kaiser": "Khizer Ahmed Biyabani",
        "richie": "Richie Shakespeare",
        "stephen": "Stephen Rigney",
        "eoin swift": "Eoin Swift",
        "swift": "Eoin Swift",
        "ashish": "Ashish Rajput",
    },
    "NTA": {
        "cathal": "Cathal Bellew",
        "cahal": "Cathal Bellew",
        "declan": "Declan Sheehan",
        "neil": "Neil",
        "mark": "Mark",
        "siobhan": "Siobhan Quinn",
    },
    "Diotima": {
        "siobhan": "Siobhan Ryan",
        "jonathan": "Jonathan Dempsey",
        "masa": "Mahsa Mahdinejad",
        "mahsa": "Mahsa Mahdinejad",
        "birva": "Birva Mehta",
    },
    "DFB": {
        "rob": "Rob Howell",
        "rob hell": "Rob Howell",
        "robert": "Rob Howell",
    },
    "Paradigm": {
        "guy": "Guy Rackham",
        "sarah": "Sarah Broderick",
        "arjit": "Arijit Sircar",
        "arijit": "Arijit Sircar",
        "arjun": "Arijit Sircar",
        "eddy": "Eddy Moretti",
        "eddie": "Eddy Moretti",
    },
    "ADAPT": {
        "kizzer": "Khizer Ahmed Biyabani",
        "khizer": "Khizer Ahmed Biyabani",
        "ashish": "Ashish Rajput",
        "declan": "Declan McKibben",
    },
    "TBS": {
        "kisito": "Kisito Futonge Nzembayie",
        "kistu": "Kisito Futonge Nzembayie",
        "stu": "Kisito Futonge Nzembayie",
        "daniel": "Daniel Coughlan",
    },
}


def cosine_sim(a, b):
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


def _score_candidate(label_emb, stored):
    """Mean of the top-3 cosine similarities against the candidate's stored
    samples. Top-K is robust to outliers (one bad sample doesn't drag the
    score down) while still requiring multiple agreeing samples for a high
    score. Falls back to max for catalogs with <3 samples."""
    sims = sorted((cosine_sim(label_emb, s) for s in stored), reverse=True)
    top = sims[:3]
    return sum(top) / len(top) if top else 0.0


def voice_match(uuid, speakers, catalog):
    """
    Compare per-speaker embeddings from this recording against voice catalog.
    Returns {label: {name, confidence, similarity, margin, runner_up, method}}
    for matched speakers. Sub-threshold or low-margin matches are dropped so
    the LLM can have a fresh attempt at identification.
    """
    emb_file = os.path.join(EMBEDDINGS_DIR, uuid + ".json")
    if not os.path.exists(emb_file) or not catalog:
        return {}

    with open(emb_file) as f:
        recording_embs = json.load(f)

    matches = {}
    for label in speakers:
        if label not in recording_embs:
            continue
        label_emb = recording_embs[label]["embedding"]

        # Score against every catalog person; sort to find best + runner-up.
        scores = []
        for name, data in catalog.items():
            stored = data.get("embeddings", [])
            if not stored:
                continue
            scores.append((name, _score_candidate(label_emb, stored)))
        if not scores:
            continue
        scores.sort(key=lambda x: -x[1])
        best_name, best_sim = scores[0]
        runner_up_name, runner_up_sim = scores[1] if len(scores) > 1 else (None, 0.0)
        margin = best_sim - runner_up_sim

        common = {
            "name": best_name,
            "similarity": round(best_sim, 3),
            "margin": round(margin, 3),
            "runner_up": runner_up_name,
            "runner_up_similarity": round(runner_up_sim, 3),
            "method": "voice",
        }
        if best_sim >= VOICE_THRESHOLD_HIGH and margin >= VOICE_MARGIN_HIGH:
            matches[label] = {**common, "confidence": "high"}
        elif best_sim >= VOICE_THRESHOLD_MEDIUM and margin >= VOICE_MARGIN_MEDIUM:
            matches[label] = {**common, "confidence": "medium"}
        # else: sub-margin or sub-threshold → fall through to LLM

    return matches


def auto_enrol(uuid, voice_matches, catalog):
    """Append the recording's embedding for any unambiguously-matched
    speaker to that person's catalog (rolling window of 20). Returns the
    list of (label, name) pairs that were enrolled."""
    emb_file = os.path.join(EMBEDDINGS_DIR, uuid + ".json")
    if not os.path.exists(emb_file):
        return []
    with open(emb_file) as f:
        recording_embs = json.load(f)

    enrolled = []
    for label, m in voice_matches.items():
        if m.get("confidence") != "high":
            continue
        if m.get("similarity", 0) < AUTO_ENROL_MIN_SIM:
            continue
        if m.get("margin", 0) < AUTO_ENROL_MIN_MARGIN:
            continue
        if label not in recording_embs:
            continue
        name = m["name"]
        person = catalog.setdefault(name, {"embeddings": []})
        person["embeddings"].append(recording_embs[label]["embedding"])
        if len(person["embeddings"]) > 20:
            person["embeddings"] = person["embeddings"][-20:]
        enrolled.append((label, name))

    if enrolled:
        with open(CATALOG_FILE, "w") as f:
            json.dump(catalog, f, indent=2)
    return enrolled


def extract_name_cues(content, attendees, category):
    """
    Scan the full transcript for lines where a speaker names other attendees.
    Returns constraint strings for the LLM prompt.

    Key logic:
    - If SPEAKER_X calls out 2+ names → SPEAKER_X is NOT any of those people
    - If SPEAKER_X calls out 1 name → SPEAKER_X likely addressed that person
    """
    expansions = CATEGORY_NAME_EXPANSIONS.get(category, {})

    # Build word → full_name lookup from attendees + nicknames
    name_lookup = {}
    for name in attendees:
        name_lookup[name.lower()] = name
        for part in name.lower().split():
            if len(part) > 2:
                name_lookup[part] = name
    for nick, full in expansions.items():
        name_lookup[nick.lower()] = full
    # Universal mishearings
    name_lookup["owen"] = next((a for a in attendees if "eoin" in a.lower()), "Eoin Lane")
    name_lookup["owen lane"] = "Eoin Lane"
    name_lookup["cahal"] = next((a for a in attendees if "cathal" in a.lower()), "Cathal Murphy")

    speaker_mentioned = defaultdict(set)
    for line in content.splitlines():
        m = re.match(r'\[(SPEAKER_\d+)\]\s+\d+:\d+\s+-\s+(.+)', line)
        if not m:
            continue
        label, text = m.group(1), m.group(2).lower()
        for word, full_name in name_lookup.items():
            if re.search(r'\b' + re.escape(word) + r'\b', text):
                speaker_mentioned[label].add(full_name)

    cues = []
    for label in sorted(speaker_mentioned.keys()):
        names = speaker_mentioned[label]
        if len(names) >= 2:
            cues.append(
                f"HARD CONSTRAINT: {label} addressed multiple people by name "
                f"({', '.join(sorted(names))}) — {label} is definitely NOT any of these people"
            )
        elif len(names) == 1:
            name = next(iter(names))
            cues.append(f"HINT: {label} mentioned {name} by name — {label} is likely NOT {name}")
    return cues


def extract_self_intros(content, attendees, category):
    """Find self-introduction patterns ("I'm X", "my name is X", "this is X")
    and validate the introduced name against the calendar invite list.
    Returns a list of constraint strings for the LLM prompt.

    Defends against WhisperX hallucinations like "Karl Bellews" (Cathal Bellew)
    or "David Spurley" (David Spurway) — when someone "introduces" themselves
    as a name that's not on the invite, we surface the closest invitee match
    so the LLM doesn't take the transcription at face value.

    Match priority per intro (first hit wins):
      1. Exact match → HARD CONSTRAINT
      2. Name expansion (per-category mishearing table) hits an invitee → HINT
      3. Unique first-name match against an invitee → HINT (likely surname mishearing)
      4. Fuzzy SequenceMatcher ratio ≥ 0.7 against an invitee → HINT
      5. None of the above + intro is >= 4 chars → NOTE (external/error)
    """
    if not attendees:
        return []

    expansions = CATEGORY_NAME_EXPANSIONS.get(category, {})
    attendee_lookup = {a.lower(): a for a in attendees}
    first_name_map = {}
    for a in attendees:
        toks = a.lower().split()
        if toks:
            first_name_map.setdefault(toks[0], []).append(a)

    # Pattern A: explicit triggers — "I'm X" / "my name is X" / "this is X"
    PAT_EXPLICIT = re.compile(
        r"\[(SPEAKER_\d+)\][^\n]*?\b(?:i'?m|i am|my name is|this is)\s+"
        r"([A-Za-z][\w']+(?:\s+[A-Za-z][\w']+){0,2})",
        re.IGNORECASE,
    )

    # Pattern B: round-table "Name, Role" introductions with a job-title
    # keyword to anchor against random commas. Catches the common pattern
    # where Whisper drops the "I'm" — e.g. "Karl Bellews, AI Business Analyst".
    ROLE_RE = (
        r"(?:director|head|ceo|cto|cfo|coo|chair|manager|analyst|architect|"
        r"executive|coordinator|consultant|officer|associate|partner|engineer|"
        r"developer|founder|principal|lead|advisor|adviser|chief|professor|"
        r"lecturer|adjunct|surveyor|controller|specialist|strategist|"
        r"researcher|scientist|owner|president|deputy|secretary|treasurer|"
        r"liaison|representative|supervisor|administrator|technician)"
    )
    PAT_NAME_ROLE = re.compile(
        r"\[(SPEAKER_\d+)\][^\n]*?[-–:]\s*"
        r"([A-Za-z][\w']+(?:\s+[A-Za-z][\w']+){0,2}),\s+"
        r"(?:[\w'\-]+\s+){0,4}?" + ROLE_RE,
        re.IGNORECASE,
    )

    cues = []
    seen = set()

    matches = list(PAT_EXPLICIT.finditer(content)) + list(PAT_NAME_ROLE.finditer(content))
    for m in matches:
        label = m.group(1)
        intro = m.group(2).strip()
        intro_lower = intro.lower()
        first_word = intro_lower.split()[0]
        key = (label, intro_lower)
        if key in seen:
            continue
        seen.add(key)

        # 1. Exact match against an invitee
        if intro_lower in attendee_lookup:
            cues.append(
                f"HARD CONSTRAINT (self-intro): {label} introduced as "
                f"\"{intro}\" which matches invitee "
                f"{attendee_lookup[intro_lower]} — therefore {label} = "
                f"{attendee_lookup[intro_lower]}"
            )
            continue

        # 2. Name expansion → invitee
        expansion = expansions.get(first_word)
        if expansion and expansion.lower() in attendee_lookup:
            cues.append(
                f"HINT (self-intro mishearing): {label} said \"I'm {intro}\""
                f" — this is the known mishearing of invitee {expansion} "
                f"(per the {category} name-expansion table). {label} = "
                f"{expansion}."
            )
            continue

        # 3. Unique first-name match against an invitee
        candidates = first_name_map.get(first_word, [])
        if len(candidates) == 1:
            cues.append(
                f"HINT (self-intro mishearing): {label} said \"I'm {intro}\""
                f" — first name matches invitee {candidates[0]} (likely "
                f"transcription mishearing of full surname). {label} = "
                f"{candidates[0]}."
            )
            continue

        # 4. Fuzzy SequenceMatcher match
        from difflib import SequenceMatcher
        best = max(
            ((a, SequenceMatcher(None, intro_lower, a.lower()).ratio())
             for a in attendees),
            key=lambda x: x[1],
        )
        if best[1] >= 0.7:
            cues.append(
                f"HINT (self-intro mishearing): {label} said \"I'm {intro}\""
                f" — fuzzy-matches invitee {best[0]} (similarity "
                f"{best[1]:.2f}). Likely transcription error; {label} = "
                f"{best[0]}."
            )
            continue

        # 5. No match — flag as external participant or transcription error.
        # Strict gate to avoid false positives from common phrases like
        # "I'm the executive" / "I'm just saying" / "I'm jumping ahead":
        #   - first letter MUST be capitalised in the original transcript
        #     (Whisper preserves case for proper nouns; common words stay
        #     lowercase mid-sentence)
        #   - intro is at least 4 chars and ≥2 words
        #   - first word not in a small filler-words denylist (belt-and-braces)
        FILLER_FIRST_WORDS = {
            "Sure", "Just", "Sorry", "Going", "Looking", "Afraid", "Trying",
            "Happy", "Really", "Actually", "Still", "Fine", "Good", "Well",
            "Right", "Very", "Here", "There", "Thinking", "Talking", "Working",
            "Doing", "Saying", "Telling", "Kind", "Sort", "Always", "Never",
            "Hoping", "Glad", "Almost", "About", "Around", "Back", "Done",
            "Free", "From", "Fully", "With", "Without", "Ready", "Also",
            "Only", "More", "Much",
        }
        first_word_orig = intro.split()[0]
        if (
            intro[:1].isupper()                 # capitalised proper-noun signal
            and len(intro) >= 4
            and " " in intro
            and first_word_orig not in FILLER_FIRST_WORDS
        ):
            cues.append(
                f"NOTE (unrecognised self-intro): {label} introduced as "
                f"\"{intro}\" but no invitee with that name. Could be an "
                f"external participant joined via the meeting link, OR a "
                f"transcription error. Don't blindly trust this name — "
                f"prefer matching against the invitee list using other cues."
            )

    return cues


def expand_names(names_str, category):
    """Expand short/informal names to full names using category context."""
    expansions = CATEGORY_NAME_EXPANSIONS.get(category, {})
    if not expansions:
        return names_str
    parts = [p.strip() for p in names_str.split(",") if p.strip()]
    expanded = []
    for part in parts:
        key = part.lower()
        expanded.append(expansions.get(key, part))
    return ", ".join(expanded)


if __name__ == "__main__":
    transcript_path = sys.argv[1]
    csv_path = sys.argv[2]

    with open(transcript_path) as f:
        content = f.read()

    # Extract UUID from header
    uuid = ""
    for line in content.splitlines()[:3]:
        if line.startswith("File:"):
            uuid = line.replace("File:", "").strip()
            uuid = re.sub(r'\.(m4a|mp3|txt)+$', '', uuid)

    if not uuid:
        print("  Could not extract UUID from transcript header — skipping", file=sys.stderr)
        sys.exit(1)

    # Find unique speaker labels
    speakers = sorted(set(re.findall(r'\[(SPEAKER_\d+|UNKNOWN)\]', content)))
    if not speakers:
        print("  No SPEAKER_XX labels found — nothing to do")
        sys.exit(0)

    # Load existing mappings
    mappings = {}
    if os.path.exists(MAPPINGS_FILE):
        with open(MAPPINGS_FILE) as f:
            mappings = json.load(f)

    # If already confirmed, just re-apply (idempotent rewrite)
    if uuid in mappings and mappings[uuid].get("confirmed"):
        print(f"  Already confirmed for {uuid} — re-applying")
        speaker_map = mappings[uuid]["mappings"]
    else:
        # Look up attendees + category + matched calendar event title from
        # the KB meeting file. The matched_event title is the strongest
        # possible identification cue when it literally names the parties
        # (e.g. "Alex & Eoin Catch up", "eoin <> declan").
        attendees = []
        category = ""
        matched_event = ""
        if os.path.exists(KB_MEETINGS_DIR):
            for fname in os.listdir(KB_MEETINGS_DIR):
                fpath = os.path.join(KB_MEETINGS_DIR, fname)
                try:
                    with open(fpath, errors="replace") as f:
                        kb_content = f.read()
                    if f"source_file: {uuid}" in kb_content:
                        cm = re.search(r'^category:\s*(\S+)', kb_content, re.MULTILINE)
                        if cm:
                            category = cm.group(1).strip()
                        # Audit-trail field added 2026-04-28 — calendar canon
                        em = re.search(r'^matched_event:\s*"([^"]+)"', kb_content, re.MULTILINE)
                        if em:
                            matched_event = em.group(1).strip()
                        am = re.search(r'\*\*Attendees:\*\*\s*\n((?:- .+\n?)+)', kb_content)
                        if am:
                            attendees = [re.sub(r'^- ', '', l).strip()
                                         for l in am.group(1).splitlines() if l.strip().startswith('- ')]
                        break
                except Exception:
                    continue

        key_people = ""
        if attendees:
            key_people = ", ".join(attendees)
            print(f"  Attendees from KB calendar ({category}): {key_people}")
        else:
            # Fall back to CSV key_people — expand short names using category context
            if os.path.exists(csv_path):
                with open(csv_path, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        row_file = row.get("filename", "").replace(".txt", "")
                        if row_file == uuid:
                            key_people = row.get("key_people", "")
                            if not category:
                                category = row.get("category", "")
                            break
            if key_people:
                expanded = expand_names(key_people, category)
                if expanded != key_people:
                    print(f"  Attendees from CSV → expanded ({category}): {expanded}")
                else:
                    print(f"  Attendees from CSV ({category}): {key_people}")
                key_people = expanded

        # Eoin Lane is always present (he made the recording) — ensure he's listed
        if key_people and "Eoin Lane" not in key_people:
            key_people = "Eoin Lane, " + key_people
        elif not key_people:
            key_people = "Eoin Lane"

        # Load speaker registry for few-shot examples
        registry = {}
        if os.path.exists(REGISTRY_FILE):
            with open(REGISTRY_FILE) as f:
                registry = json.load(f)

        registry_section = ""
        if registry:
            reg_lines = ["Known speakers confirmed from previous recordings (match speech patterns to identify them):"]
            for name, data in sorted(registry.items(), key=lambda x: -x[1].get("appearances", 0)):
                samples = data.get("samples", [])[:4]
                appearances = data.get("appearances", 0)
                if samples:
                    quoted = " | ".join(f'"{s}"' for s in samples)
                    reg_lines.append(f'- {name} ({appearances} recording(s)): {quoted}')
            registry_section = "\n".join(reg_lines)

        # ── Voice matching (fast path — no LLM needed for known speakers) ──────────
        voice_catalog = {}
        if os.path.exists(CATALOG_FILE):
            with open(CATALOG_FILE) as f:
                voice_catalog = json.load(f)

        voice_matches = voice_match(uuid, speakers, voice_catalog)
        if voice_matches:
            print(f"  Voice matches found:")
            for label, m in voice_matches.items():
                runner_up = m.get("runner_up")
                margin = m.get("margin", 0)
                ru = f", runner-up={runner_up} @ {m.get('runner_up_similarity', 0)}" if runner_up else ""
                print(f"    {label} → {m['name']} ({m['confidence']}, "
                      f"sim={m['similarity']}, margin={margin}{ru})")
            # Auto-enrol very-confident matches into catalog (compounds reliability)
            enrolled = auto_enrol(uuid, voice_matches, voice_catalog)
            for label, name in enrolled:
                print(f"    ✓ Auto-enrolled {label} → {name} (catalog now {len(voice_catalog[name]['embeddings'])} samples)")

        # Speakers not yet matched by voice → ask LLM
        unmatched = [s for s in speakers if s not in voice_matches]

        if not unmatched:
            # All speakers identified by voice — skip LLM entirely
            print(f"  All speakers matched by voice — skipping LLM")
            speaker_map = voice_matches
        else:
            # ── LLM identification for unmatched speakers ──────────────────────────
            # Extract constraints from the transcript:
            #   - name-call cues (X addressed Y → X is not Y)
            #   - self-intro validation against invite list (defends against
            #     "Karl Bellews" / "David Spurley" Whisper hallucinations)
            #   - calendar event title (catches title-named 1-on-1s like
            #     "Alex & Eoin Catch up", "eoin <> declan")
            all_attendees = [a.strip() for a in key_people.split(",") if a.strip()]
            cues = extract_name_cues(content, all_attendees, category)
            cues += extract_self_intros(content, all_attendees, category)
            # Title-based cues: when the matched calendar event title
            # contains attendee names, that's a strong signal — especially
            # for 1-on-1s. Surface as a HARD CONSTRAINT for any attendee
            # whose first name appears in the title.
            if matched_event and all_attendees:
                title_lower = matched_event.lower()
                title_named = []
                for a in all_attendees:
                    first = a.split()[0].lower() if a.strip() else ""
                    if first and len(first) > 2 and re.search(
                            r"\b" + re.escape(first) + r"\b", title_lower):
                        title_named.append(a)
                if title_named:
                    cues.insert(0, (
                        f"HARD CONSTRAINT (calendar title): the matched "
                        f"calendar event is titled \"{matched_event}\" which "
                        f"explicitly names {', '.join(title_named)} — these "
                        f"speakers ARE present in the recording with very "
                        f"high probability."
                    ))
            cues_section = ""
            if cues:
                cues_section = "\nSpeaker constraints derived from transcript:\n" + "\n".join(f"- {c}" for c in cues)

            # Note which speakers are already identified by voice
            voice_note = ""
            if voice_matches:
                resolved = ", ".join(f"{l}={m['name']}" for l, m in voice_matches.items())
                voice_note = f"\nAlready identified by voice analysis: {resolved}. Only identify the remaining speakers."

            SYSTEM_PROMPT = f"""You are identifying who is speaking in a meeting recording made by Eoin Lane, an AI consultant based in Dublin.

Eoin Lane is almost always one of the speakers — he recorded these on his iPhone. He speaks Irish English, tends to give advice, discuss clients and projects.

Your job: map each SPEAKER_XX label to a real person's full name.

Rules:
- The "Meeting title (from calendar)" is canonical. If the title literally names someone (e.g. "Alex & Eoin Catch up", "eoin <> declan"), those people ARE in the meeting — bias your IDs strongly toward them. The transcript's speaker labels and voice scores can be wrong; the calendar title is set by humans.
- Use context clues: people addressing each other by name, self-introductions, role descriptions, subject matter
- "Owen Lane" in transcript = Eoin Lane (transcription mishearing)
- "Cahal" = Cathal (transcription mishearing)
- If you're not sure, return confidence "low" — do NOT guess wildly
- If a speaker is completely unidentifiable, return null for that entry
- Always include first + last name where you know it
- Prioritise matching against the confirmed speech pattern examples below

{registry_section}

Respond with ONLY a JSON object (include ALL speaker labels, even already-identified ones):
{{
  "mappings": {{
    "SPEAKER_00": {{"name": "Eoin Lane", "confidence": "high"}},
    "SPEAKER_01": {{"name": "Cathal Murphy", "confidence": "medium"}},
    "SPEAKER_02": null
  }},
  "notes": "one sentence explaining your reasoning"
}}"""

            sample = content[:8000]
            title_line = (f"Meeting title (from calendar): \"{matched_event}\"\n"
                          if matched_event else "")
            USER_PROMPT = f"""{title_line}Confirmed attendees in this meeting: {key_people if key_people else 'unknown'}
Speaker labels present: {', '.join(speakers)}{voice_note}
{cues_section}
Transcript:
{sample}"""

            payload = {
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": USER_PROMPT}
                ],
                "stream": False,
                "keep_alive": -1,
                "options": {"temperature": 0}
            }

            # Try ollama first, fall back to Haiku via LiteLLM
            raw = None
            try:
                req = urllib.request.Request(
                    OLLAMA_URL,
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=300) as resp:
                    result = json.loads(resp.read())
                raw = result["message"]["content"].strip()
            except Exception as e:
                print(f"  Ollama unavailable ({e}), trying Haiku fallback")
                try:
                    litellm_payload = json.dumps({
                        "model": "claude-haiku-4-5",
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": USER_PROMPT}
                        ],
                        "temperature": 0,
                    }).encode()
                    req = urllib.request.Request(
                        "http://localhost:4000/v1/chat/completions",
                        data=litellm_payload,
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        result = json.loads(resp.read())
                    raw = result["choices"][0]["message"]["content"].strip()
                except Exception as e2:
                    print(f"  LiteLLM also failed: {e2}", file=sys.stderr)
                    sys.exit(1)

            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not json_match:
                # Fallback: parse prose bullet list format that deepseek-r1 sometimes returns
                # e.g. "- **SPEAKER_00**: Eoin Lane (High Confidence)"
                prose_map = {}
                for m in re.finditer(
                    r'\*\*(SPEAKER_\d+|UNKNOWN)\*\*[^:]*:\s*([A-Z][^(\n]+?)\s*\((\w+)\s*[Cc]onfidence',
                    raw
                ):
                    label, name, conf = m.group(1), m.group(2).strip(), m.group(3).lower()
                    if name.lower() not in ("null", "none", "unknown", "unidentified"):
                        prose_map[label] = {"name": name, "confidence": conf}
                if prose_map:
                    print(f"  Used prose fallback parser ({len(prose_map)} speakers)")
                    llm_map = prose_map
                    speaker_map = {**llm_map, **voice_matches}
                    mappings[uuid] = {
                        "mappings": speaker_map,
                        "confirmed": False,
                        "key_people_hint": key_people
                    }
                    with open(MAPPINGS_FILE, "w") as f:
                        json.dump(mappings, f, indent=2)
                else:
                    print(f"  Could not parse JSON from response:\n{raw[:300]}", file=sys.stderr)
                    sys.exit(1)

            if json_match:
                try:
                    parsed = json.loads(json_match.group())
                except json.JSONDecodeError as e:
                    print(f"  JSON decode error: {e}", file=sys.stderr)
                    sys.exit(1)

                llm_map = parsed.get("mappings", {})
                notes = parsed.get("notes", "")
                print(f"  LLM notes: {notes}")

                # Merge: voice matches take priority over LLM for same label
                speaker_map = {**llm_map, **voice_matches}

        mappings[uuid] = {
            "mappings": speaker_map,
            "confirmed": False,
            "key_people_hint": key_people
        }
        with open(MAPPINGS_FILE, "w") as f:
            json.dump(mappings, f, indent=2)

    # Rewrite transcript via TWO-PASS substitution to avoid cascade bugs.
    # If we did sequential `replace(old, new)` calls and two speakers swap
    # names (e.g. SPEAKER_00 was Cathal, becomes Eoin; SPEAKER_01 was Eoin,
    # becomes Cathal), the second replacement would clobber the first's
    # output. Pass 1 replaces every old label with a unique sentinel; pass 2
    # replaces sentinels with final names.
    #
    # Carry forward `applied_as` from the previously-saved mapping so the
    # LLM-fallback path can also benefit from name-to-name substitution.
    prev_mapping = (mappings.get(uuid, {}) or {}).get("mappings", {}) or {}

    new_content = content
    sentinels = []  # (label, sentinel, target)
    for label, info in speaker_map.items():
        if not info or not isinstance(info, dict):
            continue
        name = (info.get("name") or "").strip()
        confidence = info.get("confidence", "low")
        if not name:
            continue
        display = name if confidence == "high" else f"{name}?"
        target = f"[{display}]"

        # Pull applied_as from previous saved mapping if not already on info
        # (LLM-returned dicts won't carry applied_as; previous saved ones will).
        applied_as = info.get("applied_as") or prev_mapping.get(label, {}).get("applied_as")

        # What's currently in the transcript? Try [SPEAKER_XX] first, then
        # fall back to the previously-applied label form.
        placeholder = f"[{label}]"
        sentinel = f"\x00__SLOT_{label}__\x00"
        if placeholder in new_content:
            new_content = new_content.replace(placeholder, sentinel)
            sentinels.append((label, sentinel, target, "placeholder"))
        elif applied_as and f"[{applied_as}]" in new_content and applied_as != display:
            n = new_content.count(f"[{applied_as}]")
            new_content = new_content.replace(f"[{applied_as}]", sentinel)
            sentinels.append((label, sentinel, target, f"[{applied_as}]"))
            print(f"    Re-mapped {label}: [{applied_as}] → [{display}] ({n} occurrences)")

        info["applied_as"] = display

    # Pass 2: sentinels → final targets
    for label, sentinel, target, _src in sentinels:
        new_content = new_content.replace(sentinel, target)

    # Persist the applied_as so future re-runs can locate the previous label.
    if uuid in mappings:
        mappings[uuid]["mappings"] = speaker_map
        with open(MAPPINGS_FILE, "w") as f:
            json.dump(mappings, f, indent=2)

    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(transcript_path), suffix=".txt")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(new_content)
        os.replace(tmp_path, transcript_path)
    except Exception:
        os.unlink(tmp_path)
        raise

    print(f"  Speaker identification complete for {uuid}")
    for label, info in speaker_map.items():
        if info:
            marker = "" if info.get("confidence") == "high" else "?"
            print(f"    {label} → [{info['name']}{marker}] ({info['confidence']})")
        else:
            print(f"    {label} → [unidentified]")
