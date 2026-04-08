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


def update_voice_catalog(uuid, speaker_map, today_str):
    """Merge confirmed speaker embeddings from this recording into voice_catalog.json."""
    emb_file = os.path.join(EMBEDDINGS_DIR, uuid + ".json")
    if not os.path.exists(emb_file):
        return []

    with open(emb_file) as f:
        recording_embs = json.load(f)

    catalog = {}
    if os.path.exists(CATALOG_FILE):
        with open(CATALOG_FILE) as f:
            catalog = json.load(f)

    updated = []
    for label, info in speaker_map.items():
        if not info:
            continue
        name = info.get("name", "").strip()
        if not name or label not in recording_embs:
            continue

        emb = recording_embs[label]["embedding"]
        n_segs = recording_embs[label]["n_segments"]

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
