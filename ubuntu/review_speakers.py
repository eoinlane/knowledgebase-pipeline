#!/usr/bin/env python3
"""
Interactive CLI to review and confirm speaker mappings.
Run from Mac via: ssh eoin@nvidiaubuntubox python3 review_speakers.py

After confirming a mapping:
  - Removes '?' markers from the transcript
  - Marks mapping as confirmed in speaker_mappings.json
  - Extracts speech samples into speaker_registry.json (used by future identifications)

Usage:
  python3 review_speakers.py                  # iterate all unconfirmed
  python3 review_speakers.py --all            # include confirmed
  python3 review_speakers.py --prioritised    # top 5 highest-impact (defaults)
  python3 review_speakers.py --prioritised -n 10
"""

import argparse
import json, os, sys, re
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path

# Atomic JSON write — same pattern as shared/atomic_io.py. Inlined here so the
# script works whether or not shared/ is on the path.
def atomic_write_json(path, data):
    import tempfile
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
    except BaseException:
        try: os.unlink(tmp_path)
        except OSError: pass
        raise

MAPPINGS_FILE  = os.path.expanduser("~/speaker_mappings.json")
REGISTRY_FILE  = os.path.expanduser("~/speaker_registry.json")
CATALOG_FILE   = os.path.expanduser("~/voice_catalog.json")
EMBEDDINGS_DIR = os.path.expanduser("~/audio-inbox/Embeddings")
TRANS_DIR      = os.path.expanduser("~/audio-inbox/Transcriptions")
CAL_DIR        = os.path.expanduser("~/.local/share/kb/calendars")

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
        atomic_write_json(CATALOG_FILE, catalog)

    return updated


# ── Prioritised review helpers ──────────────────────────────────────────────

def _cos(a, b):
    a = np.array(a, dtype=np.float32)
    b = np.array(b, dtype=np.float32)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


def _top_k_score(emb, samples, k=3):
    if not samples:
        return 0.0
    sims = sorted((_cos(emb, s) for s in samples), reverse=True)
    return sum(sims[:k]) / len(sims[:k])


def voice_scores_against_catalog(emb, catalog, top_n=3):
    """Return top-N (name, score) tuples for an embedding."""
    scored = [(n, _top_k_score(emb, d.get("embeddings", [])))
              for n, d in catalog.items() if d.get("embeddings")]
    scored.sort(key=lambda x: -x[1])
    return scored[:top_n]


def distinctive_utterances(transcript, label, max_n=3):
    """Pull the longest non-filler utterances for `label` (or `label?`).
    Used to give a reviewer enough context to recognise a speaker."""
    pat = re.compile(r"^\[" + re.escape(label) + r"\??\]\s+\d+:\d+\s+-\s+(.+)")
    lines = []
    for line in transcript.splitlines():
        m = pat.match(line)
        if not m:
            continue
        text = m.group(1).strip()
        if len(text) >= 30 and not re.match(r"^(uh+|um+|yeah|okay|right|mm+|no+|yes|so)\b", text, re.I):
            lines.append(text)
    lines.sort(key=len, reverse=True)
    return lines[:max_n]


def find_calendar_event(uuid, recorded_at, window_min=15):
    """Return the closest calendar event within ±window_min, or None."""
    if not os.path.isdir(CAL_DIR):
        return None
    target = recorded_at
    months = {"January":1,"February":2,"March":3,"April":4,"May":5,"June":6,
              "July":7,"August":8,"September":9,"October":10,"November":11,"December":12}
    best = (None, timedelta(minutes=window_min + 1))
    for fname in sorted(os.listdir(CAL_DIR)):
        if not fname.endswith(".txt"):
            continue
        try:
            text = open(os.path.join(CAL_DIR, fname)).read()
        except Exception:
            continue
        for block in text.split("---\n"):
            t = re.search(r"^TITLE: (.+)$", block, re.MULTILINE)
            s = re.search(r"^START: (.+)$", block, re.MULTILINE)
            a = re.search(r"^ATTENDEES: (.+)$", block, re.MULTILINE)
            if not (t and s):
                continue
            sm = re.match(r"\w+ (\d{1,2}) (\w+) (\d{4}) at (\d{2}):(\d{2}):(\d{2})", s.group(1).strip())
            if not sm or sm.group(2) not in months:
                continue
            cal_ts = datetime(int(sm.group(3)), months[sm.group(2)], int(sm.group(1)),
                              int(sm.group(4)), int(sm.group(5)), int(sm.group(6)))
            delta = abs(cal_ts - target)
            if delta < best[1] and delta <= timedelta(minutes=window_min):
                attendees = [x.strip() for x in (a.group(1).split("|") if a else [])]
                best = ({"title": t.group(1).strip(), "ts": cal_ts, "attendees": attendees}, delta)
    return best[0]


def recording_timestamp(uuid):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})_(\d{2})_(\d{2})", uuid)
    if m:
        return datetime(*[int(x) for x in m.groups()])
    p = os.path.join(TRANS_DIR, uuid + ".txt")
    if os.path.exists(p):
        for line in open(p, errors="replace").read().splitlines()[:5]:
            m = re.match(r"Recorded: (\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", line)
            if m:
                return datetime(*[int(x) for x in m.groups()])
    return None


def impact_score(uuid, speaker_map, embedding_data, catalog):
    """High score = much to gain from reviewing this recording.
    Reward: many segments × low confidence × catalog cold-start."""
    score = 0
    for label, info in speaker_map.items():
        n_segs = embedding_data.get(label, {}).get("n_segments", 0)
        if n_segs < 5:
            continue
        if info is None:
            score += n_segs * 2          # unidentified — high value
            continue
        conf = info.get("confidence", "low")
        if conf == "high":
            score += 1                   # already confident — low value
        elif conf == "medium":
            score += n_segs              # mediums are most flippable
        else:
            score += n_segs * 1.5
        # Bonus if claimed name has 0–1 catalog samples (cold-start fix opportunity)
        name = info.get("name", "")
        if name and len(catalog.get(name, {}).get("embeddings", [])) <= 1:
            score += 10
    return score


def render_prioritised(uuid, data, catalog, n_idx, n_total):
    """Enhanced display: calendar event, per-speaker voice top-3, sample lines."""
    speaker_map = data.get("mappings", {}) or {}
    print(f"{'═' * 72}")
    print(f"[{n_idx}/{n_total}] Recording: {uuid}")

    # Calendar event
    ts = recording_timestamp(uuid)
    if ts:
        ev = find_calendar_event(uuid, ts)
        if ev:
            print(f"  Calendar: \"{ev['title']}\" @ {ev['ts'].strftime('%Y-%m-%d %H:%M')}")
            if ev.get("attendees"):
                print(f"  Invitees: {', '.join(ev['attendees'])}")
        else:
            print(f"  Recorded: {ts.strftime('%Y-%m-%d %H:%M')} (no calendar match)")

    # Embeddings + transcript
    emb_path = os.path.join(EMBEDDINGS_DIR, uuid + ".json")
    rec_embs = json.load(open(emb_path)) if os.path.exists(emb_path) else {}
    txt_path = os.path.join(TRANS_DIR, uuid + ".txt")
    transcript = open(txt_path).read() if os.path.exists(txt_path) else ""

    print()
    for label in sorted(speaker_map.keys()):
        info = speaker_map[label]
        n_segs = rec_embs.get(label, {}).get("n_segments", 0)
        if info:
            marker = "" if info.get("confidence") == "high" else "?"
            current = f"{info['name']}{marker} [{info.get('confidence','?')}]"
        else:
            current = "(unidentified)"
        print(f"  {label} ({n_segs} segs)  → {current}")

        # Voice top-3 against full catalog
        if rec_embs.get(label, {}).get("embedding"):
            tops = voice_scores_against_catalog(rec_embs[label]["embedding"], catalog)
            top_str = ", ".join(f"{n}={s:.2f}" for n, s in tops)
            print(f"      voice top-3: {top_str}")

        # Sample utterances
        if transcript:
            samples = distinctive_utterances(transcript, label)
            if samples:
                print(f"      utterances:")
                for s in samples:
                    print(f"        • {s[:120]}")
        print()


# ── Main ────────────────────────────────────────────────────────────────────

ap = argparse.ArgumentParser(add_help=False)
ap.add_argument("--all", action="store_true")
ap.add_argument("--prioritised", action="store_true")
ap.add_argument("-n", type=int, default=5, help="top-N for --prioritised")
ap.add_argument("-h", "--help", action="store_true")
args, _ = ap.parse_known_args()

if args.help:
    print(__doc__)
    sys.exit(0)

if not os.path.exists(MAPPINGS_FILE):
    print("No speaker_mappings.json found — no speakers identified yet.")
    sys.exit(0)

with open(MAPPINGS_FILE) as f:
    mappings = json.load(f)

candidates = {
    k: v for k, v in mappings.items()
    if args.all or not v.get("confirmed")
}

if not candidates:
    print("All mappings confirmed! Use --all to review confirmed ones.")
    sys.exit(0)

# Prioritised mode: rank candidates by impact, take top N
if args.prioritised:
    catalog = json.load(open(CATALOG_FILE)) if os.path.exists(CATALOG_FILE) else {}
    scored = []
    for uuid, data in candidates.items():
        sm = data.get("mappings", {}) or {}
        emb_path = os.path.join(EMBEDDINGS_DIR, uuid + ".json")
        rec_embs = json.load(open(emb_path)) if os.path.exists(emb_path) else {}
        if not rec_embs:
            continue
        scored.append((impact_score(uuid, sm, rec_embs, catalog), uuid, data))
    scored.sort(key=lambda x: -x[0])
    top = scored[:args.n]
    candidates = dict((u, d) for _, u, d in top)
    print(f"\nPrioritised review — top {len(candidates)} highest-impact recording(s):\n")
    # Print the ranking summary first so user knows the queue
    for i, (sc, u, _) in enumerate(top, 1):
        print(f"  {i}. {u}  (impact score: {sc})")
    print()

print(f"\n{'=' * 60}")
print(f"Speaker Mapping Review — {len(candidates)} recording(s) to review")
print(f"Commands: [y] confirm  [e] edit  [s] skip  [q] quit")
print(f"{'=' * 60}\n")

changed = False

# Loaded once for prioritised rendering
_render_catalog = None
if args.prioritised and os.path.exists(CATALOG_FILE):
    _render_catalog = json.load(open(CATALOG_FILE))

for n_idx, (uuid, data) in enumerate(candidates.items(), 1):
    speaker_map = data.get("mappings", {})
    hint = data.get("key_people_hint", "")
    confirmed = data.get("confirmed", False)
    txt_path = os.path.join(TRANS_DIR, uuid + ".txt")

    if args.prioritised:
        render_prioritised(uuid, data, _render_catalog or {}, n_idx, len(candidates))
    else:
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

        # Mark confirmed (stamp the change so reextract_stale_insights can
        # later detect that this UUID's insights need a refresh).
        mappings[uuid]["confirmed"] = True
        mappings[uuid]["mappings_updated_at"] = datetime.now().isoformat(timespec="seconds")
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

            atomic_write_json(REGISTRY_FILE, registry)

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

            # Re-extract insights — action items/decisions/follow-ups were
            # extracted under the previous speaker labels and are now stale.
            # Owners attributed to misattributed speakers (e.g. "Cathal Murphy"
            # for what's actually Alex McKenzie) get corrected on re-run.
            # Skipped silently if extract_meeting_insights or its env aren't
            # available — confirm step still completes.
            if os.path.exists(os.path.expanduser("~/extract_meeting_insights.py")):
                import subprocess as _sp
                print(f"  Re-extracting insights with corrected speaker labels...")
                csv_path = os.path.expanduser("~/audio-inbox/classification.csv")
                try:
                    r = _sp.run(
                        ["python3", os.path.expanduser("~/extract_meeting_insights.py"),
                         txt_path, csv_path],
                        capture_output=True, text=True, timeout=600,
                    )
                    if r.returncode == 0:
                        # Show the last useful line
                        for line in reversed(r.stdout.strip().splitlines()):
                            if line.strip():
                                print(f"    {line[:160]}")
                                break
                    else:
                        print(f"    insights re-extraction failed (exit {r.returncode})")
                except Exception as e:
                    print(f"    insights re-extraction skipped: {e}")
        else:
            print(f"  Warning: transcript not found at {txt_path}")

        print(f"  Confirmed!\n")
    else:
        print("  Skipped.\n")

if changed:
    atomic_write_json(MAPPINGS_FILE, mappings)
    print("Mappings saved.")
    print()
    print("To sync updated transcripts to Mac and rebuild knowledge base:")
    print("  rsync -az ~/audio-inbox/Transcriptions/ eoin@100.103.128.44:\"'/Users/eoin/Library/Mobile Documents/com~apple~CloudDocs/My Notes'/\"")
    print("  ssh eoin@100.103.128.44 python3 ~/build_knowledge_base.py")
