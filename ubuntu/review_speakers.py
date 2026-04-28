#!/usr/bin/env python3
"""
Interactive CLI to review and confirm speaker mappings.
Run from Mac via: ssh eoin@nvidiaubuntubox python3 review_speakers.py

After confirming a mapping:
  - Removes '?' markers from the transcript
  - Marks mapping as confirmed in speaker_mappings.json
  - Extracts speech samples into speaker_registry.json (used by future identifications)

Usage: python3 review_speakers.py [--all]
  --all   show already-confirmed mappings too
"""

import json, os, sys, re
import numpy as np
from datetime import date

MAPPINGS_FILE  = os.path.expanduser("~/speaker_mappings.json")
REGISTRY_FILE  = os.path.expanduser("~/speaker_registry.json")
CATALOG_FILE   = os.path.expanduser("~/voice_catalog.json")
EMBEDDINGS_DIR = os.path.expanduser("~/audio-inbox/Embeddings")
TRANS_DIR      = os.path.expanduser("~/audio-inbox/Transcriptions")

MAX_CATALOG_EMBEDDINGS = 20  # rolling window per person

MAX_SAMPLES = 15   # max stored per person
NEW_SAMPLES = 5    # max harvested from one recording


def extract_samples(content, name):
    """Pull distinctive lines spoken by `name` from a confirmed transcript."""
    samples = []
    for line in content.splitlines():
        # Match [Name] or [Name?] prefix
        m = re.match(r'^\[' + re.escape(name) + r'\??\]\s+\d+:\d+\s+-\s+(.+)', line)
        if m:
            text = m.group(1).strip()
            # Skip filler / very short lines
            if len(text) >= 40 and not re.match(r'^(uh+|um+|yeah|okay|right|mm+|no+|yes|so)\b', text, re.I):
                samples.append(text)
    # Prefer longer, more distinctive lines; shuffle slightly for variety
    samples.sort(key=len, reverse=True)
    return samples[:NEW_SAMPLES]


def update_registry(registry, name, new_samples, today_str):
    """Merge new samples into registry entry for `name`."""
    if name not in registry:
        registry[name] = {"samples": [], "appearances": 0, "last_seen": today_str}
    entry = registry[name]
    existing = set(entry["samples"])
    for s in new_samples:
        if s not in existing:
            entry["samples"].append(s)
            existing.add(s)
    # Keep most recent MAX_SAMPLES, favouring longer ones
    entry["samples"].sort(key=len, reverse=True)
    entry["samples"] = entry["samples"][:MAX_SAMPLES]
    entry["appearances"] = entry.get("appearances", 0) + 1
    entry["last_seen"] = today_str


def _integrity_check(emb, claimed_name, catalog):
    """Before adding `emb` to `claimed_name`'s catalog, check whether this
    embedding is actually closer to a DIFFERENT person already in the catalog.

    Returns (status, message):
        ("ok", None)              — looks fine, append it
        ("warn", message)         — looks suspicious, prompt user
        ("dup", message)          — exact byte-match of an existing entry

    Catches the historical failure mode where review_speakers.py confirmed
    samples that weren't actually the claimed person (e.g. Guy Rackham getting
    Eoin's voice at sim 0.85, or Declan Sheehan getting Cathal's at 0.77).
    """
    import numpy as np

    def cos(a, b):
        a = np.array(a, dtype=np.float32)
        b = np.array(b, dtype=np.float32)
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))

    # Cross-person duplicate check
    for other_name, other in catalog.items():
        for s in other.get("embeddings", []):
            if s == emb:
                if other_name == claimed_name:
                    return ("dup", f"already in {claimed_name}'s catalog (byte-identical)")
                return ("warn", f"BYTE-IDENTICAL to a sample in {other_name}'s catalog — corruption likely")

    # Score against everyone, excluding claimed_name's existing samples
    scores = []
    for other_name, other in catalog.items():
        embs = other.get("embeddings", [])
        if not embs:
            continue
        # max-over-samples (matches voice_match's top-3 mean for ≥3, max for <3)
        sims = sorted((cos(emb, s) for s in embs), reverse=True)
        score = sum(sims[:3]) / len(sims[:3]) if sims else 0.0
        scores.append((other_name, score))
    scores.sort(key=lambda x: -x[1])

    own_score = next((s for n, s in scores if n == claimed_name), None)
    best_other = next(((n, s) for n, s in scores if n != claimed_name), (None, 0))

    # Suspicious: another person scores significantly higher
    if best_other[0] and (own_score is None or best_other[1] - (own_score or 0) >= 0.10):
        return ("warn", (
            f"this embedding scores {best_other[1]:.2f} against {best_other[0]}"
            + (f" but only {own_score:.2f} against {claimed_name}"
               if own_score is not None else f" (no existing {claimed_name} samples to compare)")
            + ". Confirming will likely corrupt the catalog."
        ))

    return ("ok", None)


def update_voice_catalog(uuid, speaker_map, today_str):
    """Merge confirmed speaker embeddings from this recording into voice_catalog.json.
    Integrity-checks each candidate before appending — refuses obviously-wrong
    confirmations unless the user explicitly overrides via env var
    SKIP_INTEGRITY_CHECK=1."""
    emb_file = os.path.join(EMBEDDINGS_DIR, uuid + ".json")
    if not os.path.exists(emb_file):
        return []

    with open(emb_file) as f:
        recording_embs = json.load(f)

    catalog = {}
    if os.path.exists(CATALOG_FILE):
        with open(CATALOG_FILE) as f:
            catalog = json.load(f)

    skip_check = os.environ.get("SKIP_INTEGRITY_CHECK") == "1"
    updated = []
    for label, info in speaker_map.items():
        if not info:
            continue
        name = info.get("name", "").strip()
        if not name or label not in recording_embs:
            continue

        emb = recording_embs[label]["embedding"]
        n_segs = recording_embs[label]["n_segments"]

        # Integrity check — refuse to enrol obvious mis-attributions
        if not skip_check:
            status, msg = _integrity_check(emb, name, catalog)
            if status != "ok":
                print(f"  ⚠ Integrity check ({status}) for {label} → {name}: {msg}")
                if status == "warn":
                    try:
                        ans = input(f"    Confirm anyway? (y/N): ").strip().lower()
                    except EOFError:
                        ans = "n"
                    if ans != "y":
                        print(f"    Skipped {label} → {name} (catalog unchanged).")
                        continue
                else:  # dup
                    print(f"    Skipped {label} → {name} (already present).")
                    continue

        if name not in catalog:
            catalog[name] = {"embeddings": [], "recordings": 0,
                             "total_segments": 0, "last_seen": today_str}

        entry = catalog[name]
        entry["embeddings"].append(emb)
        if len(entry["embeddings"]) > MAX_CATALOG_EMBEDDINGS:
            entry["embeddings"] = entry["embeddings"][-MAX_CATALOG_EMBEDDINGS:]
        entry["recordings"] = entry.get("recordings", 0) + 1
        entry["total_segments"] = entry.get("total_segments", 0) + n_segs
        entry["last_seen"] = today_str
        updated.append(f"{name} ({n_segs} segs)")

    if updated:
        with open(CATALOG_FILE, "w") as f:
            json.dump(catalog, f, indent=2)

    return updated


if not os.path.exists(MAPPINGS_FILE):
    print("No speaker_mappings.json found — no speakers identified yet.")
    sys.exit(0)

with open(MAPPINGS_FILE) as f:
    mappings = json.load(f)

show_all = "--all" in sys.argv
candidates = {
    k: v for k, v in mappings.items()
    if show_all or not v.get("confirmed")
}

if not candidates:
    print("All mappings confirmed! Use --all to review confirmed ones.")
    sys.exit(0)

print(f"\n{'=' * 60}")
print(f"Speaker Mapping Review — {len(candidates)} recording(s) to review")
print(f"Commands: [y] confirm  [e] edit  [s] skip  [q] quit")
print(f"{'=' * 60}\n")

changed = False

for uuid, data in list(candidates.items()):
    speaker_map = data.get("mappings", {})
    hint = data.get("key_people_hint", "")
    confirmed = data.get("confirmed", False)

    print(f"Recording: {uuid}")
    if hint:
        print(f"Key people (from classification): {hint}")
    print(f"Status: {'✓ confirmed' if confirmed else 'unconfirmed'}")
    print("Mappings:")
    for label, info in speaker_map.items():
        if info:
            marker = "" if info.get("confidence") == "high" else "?"
            print(f"  {label:12s} → {info['name']}{marker}  [{info['confidence']}]")
        else:
            print(f"  {label:12s} → (unidentified)")

    # Peek at transcript if available
    txt_path = os.path.join(TRANS_DIR, uuid + ".txt")
    if os.path.exists(txt_path):
        with open(txt_path) as f:
            lines = [l for l in f.readlines() if not l.startswith(("File:", "Recorded:", "-"))]
        preview = "".join(lines[:6]).strip()
        if preview:
            print(f"\nTranscript preview:")
            for line in preview.splitlines()[:6]:
                print(f"  {line}")

    print()
    cmd = input("Action [y/e/s/q]: ").strip().lower()

    if cmd == 'q':
        break
    elif cmd == 's':
        print("  Skipped.\n")
        continue
    elif cmd in ('y', 'e'):
        if cmd == 'e':
            print("  Edit mappings (press Enter to keep current value):")
            for label in list(speaker_map.keys()):
                info = speaker_map[label]
                current = info["name"] if info else "unidentified"
                new_name = input(f"    {label} [{current}]: ").strip()
                if new_name:
                    if new_name.lower() in ("none", "unknown", "-", ""):
                        speaker_map[label] = None
                    else:
                        speaker_map[label] = {"name": new_name, "confidence": "confirmed"}
            mappings[uuid]["mappings"] = speaker_map

        # Mark confirmed
        mappings[uuid]["confirmed"] = True
        changed = True

        # Rewrite transcript — apply confirmed names, strip '?' markers
        if os.path.exists(txt_path):
            with open(txt_path) as f:
                content = f.read()

            for label, info in speaker_map.items():
                if not info:
                    continue
                name = info.get("name", "").strip()
                if not name:
                    continue
                content = re.sub(re.escape(f"[{name}?]"), f"[{name}]", content)
                content = content.replace(f"[{label}]", f"[{name}]")

            with open(txt_path, "w") as f:
                f.write(content)
            print(f"  Transcript rewritten — '?' markers removed.")

            # Update speaker registry with samples from this recording
            registry = {}
            if os.path.exists(REGISTRY_FILE):
                with open(REGISTRY_FILE) as f:
                    registry = json.load(f)

            today_str = date.today().isoformat()
            harvested = []
            for label, info in speaker_map.items():
                if not info:
                    continue
                name = info.get("name", "").strip()
                if not name:
                    continue
                samples = extract_samples(content, name)
                if samples:
                    update_registry(registry, name, samples, today_str)
                    harvested.append(f"{name} ({len(samples)} samples)")

            with open(REGISTRY_FILE, "w") as f:
                json.dump(registry, f, indent=2)

            if harvested:
                print(f"  Registry updated: {', '.join(harvested)}")
            else:
                print(f"  Registry: no new samples harvested (lines too short/filler)")

            # Update voice catalog with embeddings
            voice_updated = update_voice_catalog(uuid, speaker_map, today_str)
            if voice_updated:
                print(f"  Voice catalog updated: {', '.join(voice_updated)}")
            else:
                print(f"  Voice catalog: no embeddings file found for {uuid}")
        else:
            print(f"  Warning: transcript not found at {txt_path}")

        print(f"  Confirmed!\n")
    else:
        print("  Skipped.\n")

if changed:
    with open(MAPPINGS_FILE, "w") as f:
        json.dump(mappings, f, indent=2)
    print("Mappings saved.")
    print()
    print("To sync updated transcripts to Mac and rebuild knowledge base:")
    print("  rsync -az ~/audio-inbox/Transcriptions/ eoin@100.103.128.44:\"'/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes'/\"")
    print("  ssh eoin@100.103.128.44 python3 ~/build_knowledge_base.py")
