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

VOICE_THRESHOLD_HIGH   = 0.80  # cosine similarity → high confidence
VOICE_THRESHOLD_MEDIUM = 0.70  # cosine similarity → medium confidence

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


def voice_match(uuid, speakers, catalog):
    """
    Compare per-speaker embeddings from this recording against voice catalog.
    Returns {label: {name, confidence, similarity, method}} for matched speakers.
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

        best_name, best_sim = None, 0.0
        for name, data in catalog.items():
            stored = data.get("embeddings", [])
            if not stored:
                continue
            # Compare against mean of stored embeddings
            mean_emb = np.mean(stored, axis=0)
            sim = cosine_sim(label_emb, mean_emb)
            if sim > best_sim:
                best_sim, best_name = sim, name

        if best_sim >= VOICE_THRESHOLD_HIGH:
            matches[label] = {"name": best_name, "confidence": "high",
                              "similarity": round(best_sim, 3), "method": "voice"}
        elif best_sim >= VOICE_THRESHOLD_MEDIUM:
            matches[label] = {"name": best_name, "confidence": "medium",
                              "similarity": round(best_sim, 3), "method": "voice"}

    return matches


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
            uuid = re.sub(r'\.(m4a|txt)$', '', uuid)

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
        # Look up attendees + category from KB meeting file
        attendees = []
        category = ""
        if os.path.exists(KB_MEETINGS_DIR):
            for fname in os.listdir(KB_MEETINGS_DIR):
                fpath = os.path.join(KB_MEETINGS_DIR, fname)
                try:
                    with open(fpath, errors="replace") as f:
                        kb_content = f.read()
                    if f"source_file: {uuid}" in kb_content:
                        # Extract category from frontmatter
                        cm = re.search(r'^category:\s*(\S+)', kb_content, re.MULTILINE)
                        if cm:
                            category = cm.group(1).strip()
                        # Extract **Attendees:** bullet list (from calendar)
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
                print(f"    {label} → {m['name']} ({m['confidence']}, sim={m['similarity']})")

        # Speakers not yet matched by voice → ask LLM
        unmatched = [s for s in speakers if s not in voice_matches]

        if not unmatched:
            # All speakers identified by voice — skip LLM entirely
            print(f"  All speakers matched by voice — skipping LLM")
            speaker_map = voice_matches
        else:
            # ── LLM identification for unmatched speakers ──────────────────────────
            # Extract name-call cues from full transcript
            all_attendees = [a.strip() for a in key_people.split(",") if a.strip()]
            cues = extract_name_cues(content, all_attendees, category)
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
            USER_PROMPT = f"""Confirmed attendees in this meeting: {key_people if key_people else 'unknown'}
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

    # Rewrite transcript
    new_content = content
    for label, info in speaker_map.items():
        if not info or not isinstance(info, dict):
            continue
        name = (info.get("name") or "").strip()
        confidence = info.get("confidence", "low")
        if not name:
            continue
        display = name if confidence == "high" else f"{name}?"
        new_content = new_content.replace(f"[{label}]", f"[{display}]")

    with open(transcript_path, "w") as f:
        f.write(new_content)

    print(f"  Speaker identification complete for {uuid}")
    for label, info in speaker_map.items():
        if info:
            marker = "" if info.get("confidence") == "high" else "?"
            print(f"    {label} → [{info['name']}{marker}] ({info['confidence']})")
        else:
            print(f"    {label} → [unidentified]")
