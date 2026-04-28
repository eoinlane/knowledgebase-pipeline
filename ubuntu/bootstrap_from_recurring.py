#!/usr/bin/env python3
"""
Bootstrap voice-catalog samples from recurring calendar events.

Strategy:
  • Find calendar events matching a title pattern (or auto-discover recurring
    titles)
  • For each instance, find the recording in a ±15 min window
  • Anchor Eoin via voice match (his catalog is robust)
  • For each remaining speaker, try to identify via existing voice catalogs
    of other invitees; or — if the recording has exactly N speakers and the
    invite has exactly N attendees and N-1 of them are voice-confirmed — by
    elimination, the last speaker is the last attendee
  • Enrol identified speakers via integrity check (catches obvious corruption)

Usage:
    bootstrap_from_recurring.py                         # dry-run, all recurring titles
    bootstrap_from_recurring.py --title "Weekly catchup with Delcan"
    bootstrap_from_recurring.py --apply                 # actually enrol
    bootstrap_from_recurring.py --min-occurrences 5     # tighter recurring threshold
"""
import argparse
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# Allow running from either deployed or repo location
PIPELINE_DIR = Path(__file__).resolve().parent.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(PIPELINE_DIR / "ubuntu"))
from identify_speakers import _score_candidate

VC_PATH = Path.home() / "voice_catalog.json"
EMB_DIR = Path.home() / "audio-inbox" / "Embeddings"
TRANS_DIR = Path.home() / "audio-inbox" / "Transcriptions"
CAL_DIR = Path.home() / ".local" / "share" / "kb" / "calendars"

EOIN_NAMES = {"eoin lane", "eoin.lane@adaptcentre.ie", "eoinlane@gmail.com",
              "eoin.lane@nationaltransport.ie"}
EMAIL_TO_NAME = {
    "shji@tcd.ie": "Shunyu Ji",
    "rigneyst@tcd.ie": "Stephen Rigney",
    "stephen.rigney@adaptcentre.ie": "Stephen Rigney",
    "akjha@tcd.ie": "Ashish Kumar Jha",
    "khizerahmed.biyabani@adaptcentre.ie": "Khizer Ahmed Biyabani",
    "declan.mckibben@adaptcentre.ie": "Declan McKibben",
    "edmond.oconnor@adaptcentre.ie": "Edmond O'Connor",
    "jamie.cudden@dublincity.ie": "Jamie Cudden",
    "christopher.kelly@dublincity.ie": "Christopher Kelly",
    "richardm.shakespeare@dublincity.ie": "Richie Shakespeare",
    "robert.howell@dublincity.ie": "Rob Howell",
    "alan.dooley@limerick.ie": "Alan Dooley",
    "guyrackham@gmail.com": "Guy Rackham",
}
EOIN_ANCHOR_MIN = 0.80     # need this confident to trust Eoin identification
ID_MIN = 0.70              # min sim to claim a speaker is a known person
ID_MARGIN = 0.10           # second-best must be ID_MARGIN below best
ENROL_MIN_SEGMENTS = 5     # don't enrol speakers with too few audio segments
WINDOW_MIN = 15

MONTHS = {"January":1,"February":2,"March":3,"April":4,"May":5,"June":6,
          "July":7,"August":8,"September":9,"October":10,"November":11,"December":12}


def normalise_attendee(s):
    s_low = s.lower().strip()
    if s_low in EOIN_NAMES:
        return "Eoin Lane"
    if s_low in EMAIL_TO_NAME:
        return EMAIL_TO_NAME[s_low]
    if "@" in s:
        return None  # unknown email — skip
    return s.strip()


def parse_iso(s):
    m = re.match(r"\w+ (\d{1,2}) (\w+) (\d{4}) at (\d{2}):(\d{2}):(\d{2})", s.strip())
    if not m or m.group(2) not in MONTHS:
        return None
    return datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)))


def load_events():
    events = []
    for f in sorted(CAL_DIR.glob("*.txt")):
        for block in f.read_text(errors="replace").split("---\n"):
            t = re.search(r"^TITLE: (.+)$", block, re.MULTILINE)
            s = re.search(r"^START: (.+)$", block, re.MULTILINE)
            a = re.search(r"^ATTENDEES: (.+)$", block, re.MULTILINE)
            if not (t and s and a):
                continue
            ts = parse_iso(s.group(1))
            if not ts:
                continue
            raw = [x.strip() for x in a.group(1).split("|") if x.strip()]
            attendees = [normalise_attendee(x) for x in raw]
            attendees = [x for x in attendees if x]
            events.append({
                "title": t.group(1).strip(),
                "ts": ts,
                "attendees": list(dict.fromkeys(attendees)),  # dedup, preserve order
            })
    return events


def recording_timestamp(uuid):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})_(\d{2})_(\d{2})", uuid)
    if m:
        return datetime(*[int(x) for x in m.groups()])
    p = TRANS_DIR / f"{uuid}.txt"
    if p.exists():
        for line in p.read_text(errors="replace").splitlines()[:5]:
            m = re.match(r"Recorded: (\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", line)
            if m:
                return datetime(*[int(x) for x in m.groups()])
    return None


def find_recording(ts, recordings_by_ts):
    """Return UUID of recording closest to `ts` within ±WINDOW_MIN, else None."""
    best = (None, timedelta(minutes=WINDOW_MIN + 1))
    for uuid, rec_ts in recordings_by_ts.items():
        delta = abs(rec_ts - ts)
        if delta < best[1] and delta <= timedelta(minutes=WINDOW_MIN):
            best = (uuid, delta)
    return best[0]


def identify_speakers(rec, attendees, vc):
    """Try to identify each speaker in `rec` as one of `attendees`.

    Returns: {speaker_label: (name, score, margin)}.
    Eoin must be confidently identified or we abort the whole recording.
    """
    speakers = [k for k, v in rec.items()
                if isinstance(v, dict) and v.get("embedding")]
    if not speakers:
        return None
    speakers.sort()
    n_speakers = len(speakers)
    n_attendees = len(attendees)

    # Score every speaker against every attendee's catalog
    scores = {}
    for sp in speakers:
        emb = rec[sp]["embedding"]
        scores[sp] = {}
        for name in attendees:
            cat = vc.get(name, {}).get("embeddings", [])
            scores[sp][name] = _score_candidate(emb, cat) if cat else 0.0

    # Step 1: anchor Eoin
    if not scores:
        return None
    eoin_speaker = max(scores, key=lambda s: scores[s].get("Eoin Lane", 0))
    if scores[eoin_speaker].get("Eoin Lane", 0) < EOIN_ANCHOR_MIN:
        return None  # Eoin not confidently identified — abort

    assignments = {eoin_speaker: ("Eoin Lane",
                                   scores[eoin_speaker]["Eoin Lane"], None)}
    remaining_speakers = [s for s in speakers if s != eoin_speaker]
    remaining_attendees = [a for a in attendees if a != "Eoin Lane"]

    # Step 2: voice-match each remaining speaker against remaining attendees
    for sp in list(remaining_speakers):
        cands = sorted(
            ((name, scores[sp][name]) for name in remaining_attendees),
            key=lambda x: -x[1],
        )
        if len(cands) >= 2:
            best_name, best_score = cands[0]
            ru_score = cands[1][1]
            margin = best_score - ru_score
        else:
            best_name, best_score = cands[0] if cands else (None, 0)
            margin = best_score
        if best_name and best_score >= ID_MIN and margin >= ID_MARGIN:
            assignments[sp] = (best_name, best_score, margin)
            remaining_speakers.remove(sp)
            remaining_attendees.remove(best_name)

    # Step 3: by-elimination — if there's exactly 1 speaker and 1 attendee left,
    # AND the recording has no extras, AND the speaker has enough segments,
    # claim them.
    if (len(remaining_speakers) == 1 and len(remaining_attendees) == 1
            and n_speakers == n_attendees):
        sp = remaining_speakers[0]
        a = remaining_attendees[0]
        n_segs = rec[sp].get("n_segments", 0)
        if n_segs >= ENROL_MIN_SEGMENTS:
            assignments[sp] = (a, scores[sp].get(a, 0.0), "by-elimination")

    return assignments


def already_in_catalog(emb, vc):
    """Return (name, idx) if `emb` is byte-identical to an existing catalog
    sample anywhere in the catalog, else None."""
    for name, data in vc.items():
        for i, s in enumerate(data.get("embeddings", [])):
            if s == emb:
                return (name, i)
    return None


def integrity_ok(emb, claimed, vc):
    # Already enrolled — skip rather than duplicate
    dup = already_in_catalog(emb, vc)
    if dup:
        return False, "duplicate", (dup[0], 1.0)

    own = _score_candidate(emb, vc.get(claimed, {}).get("embeddings", []))
    best_other = max(
        ((n, _score_candidate(emb, d["embeddings"]))
         for n, d in vc.items() if n != claimed and d.get("embeddings")),
        key=lambda x: x[1], default=(None, 0.0),
    )
    return best_other[1] - own < 0.10, own, best_other


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", help="Calendar event title to match (substring, case-insensitive)")
    ap.add_argument("--apply", action="store_true", help="Actually enrol (default: dry-run)")
    ap.add_argument("--min-occurrences", type=int, default=3,
                    help="Title must recur this many times to be considered (default: 3)")
    args = ap.parse_args()

    print("Loading calendar events...", flush=True)
    events = load_events()

    # Group by title
    by_title = defaultdict(list)
    for e in events:
        by_title[e["title"]].append(e)

    if args.title:
        recurring = {t: evs for t, evs in by_title.items()
                     if args.title.lower() in t.lower()}
    else:
        recurring = {t: evs for t, evs in by_title.items()
                     if len(evs) >= args.min_occurrences}

    print(f"Recurring event titles: {len(recurring)}\n", flush=True)

    # Build recording index
    recordings_by_ts = {}
    for emb_file in EMB_DIR.glob("*.json"):
        ts = recording_timestamp(emb_file.stem)
        if ts:
            recordings_by_ts[emb_file.stem] = ts

    print(f"Recordings indexed: {len(recordings_by_ts)}\n", flush=True)

    vc = json.load(VC_PATH.open())
    plan = []
    skipped_reasons = defaultdict(int)

    for title, evs in sorted(recurring.items(), key=lambda x: -len(x[1])):
        for ev in evs:
            uuid = find_recording(ev["ts"], recordings_by_ts)
            if not uuid:
                skipped_reasons["no recording"] += 1
                continue
            rec = json.load((EMB_DIR / f"{uuid}.json").open())
            if "Eoin Lane" not in ev["attendees"]:
                skipped_reasons["Eoin not on invite"] += 1
                continue
            assignments = identify_speakers(rec, ev["attendees"], vc)
            if assignments is None:
                skipped_reasons["Eoin not voice-anchored"] += 1
                continue
            for sp, (name, score, extra) in assignments.items():
                if name == "Eoin Lane":
                    continue  # Eoin's catalog is already strong
                n_segs = rec[sp].get("n_segments", 0)
                if n_segs < ENROL_MIN_SEGMENTS:
                    skipped_reasons[f"too few segments ({n_segs})"] += 1
                    continue
                # By-elimination cases with very weak own-score will fail
                # the integrity check anyway. Filter them upstream so the
                # plan output doesn't surface noise.
                if extra == "by-elimination" and score < 0.30:
                    skipped_reasons[f"by-elimination too weak ({score:.2f})"] += 1
                    continue
                # Already in catalog (byte-identical) — skip duplicates
                if already_in_catalog(rec[sp]["embedding"], vc):
                    skipped_reasons["already in catalog"] += 1
                    continue
                plan.append({
                    "uuid": uuid,
                    "ts": ev["ts"],
                    "title": title,
                    "speaker": sp,
                    "name": name,
                    "score": score,
                    "extra": extra,
                    "emb": rec[sp]["embedding"],
                })

    # De-dupe: same (uuid, name) — keep first
    seen = set()
    deduped = []
    for p in plan:
        key = (p["uuid"], p["name"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    plan = deduped

    # Per-person summary
    by_name = defaultdict(list)
    for p in plan:
        by_name[p["name"]].append(p)

    print(f"=== Enrolment plan ({len(plan)} candidates) ===\n")
    for name in sorted(by_name, key=lambda n: -len(by_name[n])):
        cur = len(vc.get(name, {}).get("embeddings", []))
        print(f"  {name:<30}  +{len(by_name[name])} samples (currently {cur})")
        for p in by_name[name]:
            extra = (f" margin={p['extra']:.3f}" if isinstance(p['extra'], (int, float))
                     else f" ({p['extra']})" if p['extra'] else "")
            print(f"      {p['ts'].strftime('%Y-%m-%d %H:%M')}  {p['uuid'][:8]}/{p['speaker']}"
                  f"  score={p['score']:.3f}{extra}  [{p['title'][:40]}]")
    print()
    if skipped_reasons:
        print("Skipped:")
        for r, n in sorted(skipped_reasons.items(), key=lambda x: -x[1]):
            print(f"  {n:>4} × {r}")

    if not args.apply:
        print("\nDRY RUN — pass --apply to actually enrol.")
        return

    # Apply
    backup = VC_PATH.with_suffix(f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    shutil.copy(VC_PATH, backup)
    print(f"\nBackup: {backup}")

    vc = json.load(VC_PATH.open())
    applied = refused = 0
    for p in plan:
        ok, own, best = integrity_ok(p["emb"], p["name"], vc)
        if not ok:
            print(f"  REFUSED {p['uuid'][:8]}/{p['speaker']} → {p['name']}: "
                  f"{best[0]} scores {best[1]:.3f} > own {own:.3f}")
            refused += 1
            continue
        if p["name"] not in vc:
            vc[p["name"]] = {"embeddings": [], "recordings": 0,
                             "total_segments": 0,
                             "last_seen": p["ts"].strftime("%Y-%m-%d")}
        vc[p["name"]]["embeddings"].append(p["emb"])
        if len(vc[p["name"]]["embeddings"]) > 20:
            vc[p["name"]]["embeddings"] = vc[p["name"]]["embeddings"][-20:]
        vc[p["name"]]["recordings"] = vc[p["name"]].get("recordings", 0) + 1
        vc[p["name"]]["last_seen"] = p["ts"].strftime("%Y-%m-%d")
        applied += 1

    json.dump(vc, VC_PATH.open("w"), indent=2)
    print(f"\nApplied: {applied}; refused {refused}.")
    print(f"Catalog total: {sum(len(d['embeddings']) for d in vc.values())} samples / {len(vc)} people.")


if __name__ == "__main__":
    main()
