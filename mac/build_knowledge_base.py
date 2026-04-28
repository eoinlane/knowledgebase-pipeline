"""
Build a markdown knowledge base from meeting notes, transcripts, and calendar data.
Output: ~/knowledge_base/ — one .md file per note, plus index files.
"""

import csv, io, json, re, os, subprocess, sys, time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

PIPELINE_DIR = os.environ.get("PIPELINE_DIR", os.path.expanduser("~/knowledgebase-pipeline"))
if os.path.isdir(PIPELINE_DIR) and PIPELINE_DIR not in sys.path:
    sys.path.insert(0, PIPELINE_DIR)
try:
    from shared.config import PERSON_CATEGORY, KEEP_CATEGORIES
except ImportError:
    PERSON_CATEGORY = {}
    KEEP_CATEGORIES = set()


NOTES_DIR = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/My Notes"
)
CSV_PATH = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/My Notes Analysis/classification.csv"
)
OUTPUT_DIR = os.path.expanduser("~/knowledge_base")
os.makedirs(OUTPUT_DIR, exist_ok=True)
for sub in ("meetings", "people", "topics"):
    os.makedirs(os.path.join(OUTPUT_DIR, sub), exist_ok=True)

# ── Copy iCloud files to /tmp before reading ──────────────────────────────────
# iCloud holds kernel-level exclusive locks on files during sync, causing
# EDEADLK on any read attempt. Rsync to /tmp first — /tmp is never locked.
_TMP_NOTES = "/tmp/kb_notes_build"
_TMP_ANALYSIS = "/tmp/kb_analysis_build"
os.makedirs(_TMP_NOTES, exist_ok=True)
os.makedirs(_TMP_ANALYSIS, exist_ok=True)
subprocess.run(["rsync", "-a", "--ignore-errors", NOTES_DIR + "/", _TMP_NOTES + "/"], capture_output=True)
subprocess.run(["rsync", "-a", "--ignore-errors",
    os.path.dirname(CSV_PATH) + "/", _TMP_ANALYSIS + "/"], capture_output=True)
NOTES_DIR = _TMP_NOTES
CSV_PATH = os.path.join(_TMP_ANALYSIS, "classification.csv")

# Rsync insights from Ubuntu (if reachable)
_TMP_INSIGHTS = "/tmp/kb_insights"
os.makedirs(_TMP_INSIGHTS, exist_ok=True)
subprocess.run(["rsync", "-az", "-e", "ssh -o ConnectTimeout=5 -o BatchMode=yes",
    "eoin@nvidiaubuntubox:~/audio-inbox/Insights/", _TMP_INSIGHTS + "/"],
    capture_output=True, timeout=30)

# ── Load confirmed speaker mappings (from Ubuntu, synced by sync-knowledge-base.sh)
# Used to disambiguate overlapping calendar events: when timestamp scoring is
# close, the event whose invitees match this recording's confirmed voice IDs
# wins. Falls back gracefully if file is missing or stale.
_SPEAKER_MAPPINGS_PATH = os.path.expanduser("~/.local/share/kb/speaker_mappings.json")
SPEAKER_MAPPINGS = {}
if os.path.exists(_SPEAKER_MAPPINGS_PATH):
    try:
        with open(_SPEAKER_MAPPINGS_PATH) as f:
            SPEAKER_MAPPINGS = json.load(f)
    except (json.JSONDecodeError, OSError):
        SPEAKER_MAPPINGS = {}


def confirmed_voice_names(uuid):
    """Return set of confirmed-voice attendee names for a recording.
    Empty set if no entry, not confirmed, or no high-confidence voice IDs.
    Excludes Eoin (always present) and `?`-marked low-confidence guesses."""
    rec = SPEAKER_MAPPINGS.get(uuid, {})
    if not isinstance(rec, dict) or not rec.get("confirmed"):
        return set()
    names = set()
    for spk, m in (rec.get("mappings") or {}).items():
        if not isinstance(m, dict):
            continue
        applied = m.get("applied_as") or m.get("name") or ""
        # Skip uncertainty markers and Eoin (he's always there, not a tiebreaker)
        if not applied or applied.endswith("?") or applied == "Eoin Lane":
            continue
        names.add(applied)
    return names


def icloud_read(path, **kwargs):
    """Read a file — now always from /tmp, never from iCloud Drive."""
    with open(path, **kwargs) as f:
        return f.read()


def icloud_open(path, **kwargs):
    """Open a file — now always from /tmp, never from iCloud Drive."""
    return open(path, **kwargs)

# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_dt(s):
    try:
        return datetime.strptime(s.replace(" at ", " "), "%A %d %B %Y %H:%M:%S")
    except:
        return None

def load_cal_file(path):
    try:
        with open(path, errors="replace") as f:
            content = f.read()
        # Fix garbled apostrophes from Exchange calendar export (e.g. L\xd5Estrange → L'Estrange)
        content = content.replace("\xd5", "'").replace("\ufffd", "'")
    except FileNotFoundError:
        return []
    events = []
    for block in content.strip().split("---\n"):
        if not block.strip():
            continue
        ev = {}
        for line in block.strip().splitlines():
            idx = line.find(":")
            if idx > 0:
                ev[line[:idx].strip()] = line[idx + 1:].strip()
        if ev.get("START"):
            dt = parse_dt(ev["START"])
            ev["_dt"] = dt
            ev["_date"] = dt.date() if dt else None
            # END can be either same-day "HH:MM:SS" or full "Day DD Month YYYY HH:MM:SS"
            end_str = ev.get("END", "").strip()
            end_dt = None
            if end_str and dt:
                if " at " in end_str:
                    end_dt = parse_dt(end_str)
                else:
                    # Same-day end: combine with event's date
                    try:
                        h, m, s = [int(x) for x in end_str.split(":")]
                        end_dt = dt.replace(hour=h, minute=m, second=s)
                    except (ValueError, AttributeError):
                        end_dt = None
            ev["_end_dt"] = end_dt
            events.append(ev)
    return events


def load_cal_pipe_file(path):
    """Load |||‑delimited calendar export (AppleScript live export format).
    Format: calendar|||title|||datetime|||attendee1|attendee2|...
    """
    try:
        with open(path, errors="replace") as f:
            content = f.read()
        content = content.replace("\xd5", "'").replace("\ufffd", "'")
    except FileNotFoundError:
        return []
    events = []
    for line in content.strip().splitlines():
        parts = line.split("|||")
        if len(parts) < 4:
            continue
        cal_name = parts[0].strip()
        title = parts[1].strip()
        dt_str = parts[2].strip()
        attendee_str = parts[3].strip().rstrip("|")
        dt = parse_dt(dt_str)
        ev = {
            "TITLE": title,
            "START": dt_str,
            "ATTENDEES": attendee_str,
            "CALENDAR": cal_name,
            "_dt": dt,
            "_date": dt.date() if dt else None,
        }
        events.append(ev)
    return events

NAME_MAP = {
    "arijit": "arijit", "arjit": "arijit",
    "eddie": "eddy", "eddy": "eddy", "moretti": "eddy",
    "guy": "rackham", "rackham": "rackham",
    "sarah": "sarah", "broderick": "sarah",
    "siobhan": "siobhan",
    "jonathan": "dempsey", "dempsey": "dempsey",
    "tom": "pollock", "pollock": "pollock",
    "ian": "ian",
    "long": "long", "thanh": "long",
    "daniel": "daniel", "fernandez": "daniel",
    "carl": "carl", "vogel": "carl",
    "masa": "mahsa", "mahsa": "mahsa", "mahdinejad": "mahsa",
    "birva": "birva", "mehta": "birva",
    "ann": "ann", "devitt": "ann",
    "greg": "greg", "carey": "greg",
    "declan": "declan", "mckibben": "declan",
    "richie": "shakespeare", "shakespeare": "shakespeare",
    "jamie": "jamie", "cudden": "jamie",
    "khizer": "khizer",
    "ashish": "ashish",
    "stephen": "stephen", "rigney": "stephen",
    "nicola": "nicola", "graham": "nicola",
    "aidan": "aidan", "blighe": "aidan",
    "robert": "robert", "ross": "robert",
    "richard": "richard",
    "edmond": "edmond",
    "alex": "alex", "mckenzie": "alex",
    "jeremy": "jeremy", "ryan": "ryan",
    "dermot": "dermot", "gara": "dermot",
    "philip": "philip", "estrange": "philip", "cregan": "philip",
    "tomas": "tomas", "kelly": "tomas",
    "hugh": "hugh",
    "fergus": "fergus", "heneghan": "fergus",
    "cathal": "cathal", "bellew": "cathal",
    "neil": "neil", "sutch": "neil",
    "prasanth": "prasanth",
    "gerard": "gerard", "cuddihy": "gerard",
    "rob": "rob", "reid": "rob",
    "audrey": "audrey",
    "ger": "ger", "regan": "ger",
    "orlagh": "orlagh",
    "kevin": "kevin",
    "dominic": "dominic", "hannigan": "dominic",
    "john": "john", "robinson": "john",
    "fionn": "fionn",
    "claire": "claire", "mcloughney": "claire",
    "conor": "conor",
    "mark": "mark",
    "colm": "colm",
    "mariano": "mariano",
    "kieran": "kieran",
    "fergal": "fergal",
    "natasha": "natasha",
    "rachel": "rachel",
    "frank": "frank",
    "eoin": "eoin", "owen": "eoin",
    "dave": "dave", "hayes": "dave",
    "cahal": "cathal",
}

NOISE = {
    "eoin", "meeting", "call", "catch", "teams", "microsoft", "zoom",
    "google", "meet", "lane", "noval", "consultancy", "with", "from",
    "update", "weekly", "check", "prep", "review", "intro", "session",
    "nationaltransport", "adaptcentre", "learnovate", "tcd", "gmail",
    "paradigmshiftsystems", "thisisorg", "dublincity", "openai",
}

def tokens(text):
    result = set()
    for w in re.findall(r"[a-z']+", text.lower()):
        mapped = NAME_MAP.get(w, w if len(w) > 3 else None)
        if mapped and mapped not in NOISE:
            result.add(mapped)
    return result

def mtg_tokens(e):
    return (tokens(e.get("ATTENDEES", "")) | tokens(e.get("TITLE", ""))) - NOISE

def extract_action_items(transcript):
    """Heuristic extraction of action items from transcript text."""
    actions = []
    patterns = [
        r"(?:i'll|i will|i'm going to|i need to|let me|i should)\s+([^.!?\n]{10,80})",
        r"(?:we'll|we will|we're going to|we need to|we should)\s+([^.!?\n]{10,80})",
        r"(?:action[:\s]+|follow.up[:\s]+|next step[s]?[:\s]+|todo[:\s]+)([^.!?\n]{10,80})",
        r"(?:can you|could you|would you)\s+([^.!?\n]{10,80})",
        r"(?:by (?:end of|next|this) (?:week|month|friday|monday|tuesday|wednesday|thursday))[^\n]{0,60}",
        r"(?:send|share|schedule|book|arrange|prepare|write|draft|review|check)\s+(?:the|a|an|that|those|it)?\s*([^.!?\n]{10,70})",
    ]
    seen = set()
    for line in transcript.splitlines():
        line_lower = line.lower()
        for pattern in patterns:
            for m in re.finditer(pattern, line_lower):
                item = m.group(0).strip()
                item = re.sub(r'^\[speaker_\d+\]\s*\d+:\d+\s*-\s*', '', item, flags=re.I)
                item = item.strip().capitalize()
                if len(item) > 15 and item not in seen:
                    seen.add(item)
                    actions.append(item)
    return actions[:20]  # cap at 20

def format_attendees_md(attendee_str):
    """Format attendee pipe-delimited string to markdown list."""
    lines = []
    seen = set()
    for a in attendee_str.split("|"):
        a = a.strip()
        if not a or a == "<>":
            continue
        # Parse "Name<email>"
        m = re.match(r"^(.*?)<([^>]+)>$", a)
        if m:
            name = m.group(1).strip().strip('"')
            email = m.group(2).strip()
            if email and email not in seen:
                seen.add(email)
                if name and name.lower() not in ("", email.lower()):
                    lines.append(f"- **{name}** ({email})")
                else:
                    lines.append(f"- {email}")
        elif a not in seen:
            seen.add(a)
            lines.append(f"- {a}")
    return "\n".join(lines)

def slugify(text, max_len=60):
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:max_len]

# ── Load calendar events ─────────────────────────────────────────────────────
# Calendars live in ~/.local/share/kb/calendars/ (stable, survives reboot).
# Used to live in /tmp/, which macOS clears on reboot — every time the Mac
# rebooted between 4am and the next CSV-driven sync, the build silently lost
# calendar data and produced meeting files without `attendees:`. Fixed by
# moving to a non-volatile location 2026-04-27.
import pathlib as _pl
CAL_DIR = _pl.Path.home() / ".local" / "share" / "kb" / "calendars"
print(f"Loading calendar data from {CAL_DIR}...")
all_events = []
for fname in [
    "cal_eoinlane.txt",
    "cal_work.txt",
    "calendar_events.txt",
    "cal_extra_15.txt",
    "cal_nta.txt",
    "cal_adapt.txt",
    "cal_personal.txt",
    "cal_home.txt",
]:
    all_events += load_cal_file(str(CAL_DIR / fname))

# Load |||‑delimited Apple Calendar exports (live AppleScript format)
for fname in [
    "cal_2025_1_January_2025.txt",
    "cal_2025_1_July_2025.txt",
    "cal_2025_events.txt",
    "cal_2026_events.txt",
    "cal_all_events.txt",
]:
    all_events += load_cal_pipe_file(str(CAL_DIR / fname))

# Deduplicate
seen_keys = set()
unique_events = []
for e in all_events:
    key = (e.get("TITLE", "").strip().lower()[:40], e.get("START", "")[:16])
    if key not in seen_keys:
        seen_keys.add(key)
        unique_events.append(e)
print(f"  {len(unique_events)} unique calendar events")
if len(unique_events) == 0:
    print(
        f"\n  ⚠️  WARNING: 0 calendar events loaded from {CAL_DIR}.\n"
        f"     Attendee matching will be DISABLED — every meeting built in this\n"
        f"     run will have no `attendees:` frontmatter. Run\n"
        f"     bash ~/.local/bin/export-calendars.sh to regenerate.\n",
        flush=True,
    )

# ── Load notes CSV ────────────────────────────────────────────────────────────
print("Loading notes CSV...")
notes = []
with io.StringIO(icloud_read(CSV_PATH)) as f:
    for row in csv.DictReader(f):
        if not row["date"] or row["category"] == "other:blank":
            continue
        try:
            dt = datetime.strptime(row["date"].split(" ")[0], "%Y-%m-%d").date()
        except:
            continue
        notes.append(row | {"_date": dt})
print(f"  {len(notes)} notes to process")

# ── Match notes to calendar events ───────────────────────────────────────────
DUBLIN_TZ = ZoneInfo("Europe/Dublin")
UTC_TZ = ZoneInfo("UTC")


def find_meetings_by_time(recording_dt, cal_events, voice_names=None):
    """Match a recording to calendar events by timestamp.
    recording_dt is naive UTC (from transcript header).
    Calendar event _dt is naive Dublin local time (from AppleScript/icalBuddy).
    Match if recording is within 30 min before event start, or up to 60 min after
    (covers recordings started just before or during a meeting; widened from
    15-before to 30-before because users sometimes hit record minutes ahead).

    `voice_names` (optional): set of confirmed voice-IDed attendee names from
    speaker_mappings. When non-empty, candidates whose invitees overlap with
    these names get a strong bonus — disambiguates overlapping calendar events
    (a recording can sit between two events in time but only one will match the
    voices in the room).

    Returns a list of (delta_seconds_abs, score_negative, event) tuples,
    sorted best-first. The first element is the canonical match.
    """
    if not recording_dt:
        return []
    rec_aware = recording_dt.replace(tzinfo=UTC_TZ)
    rec_dublin = rec_aware.astimezone(DUBLIN_TZ).replace(tzinfo=None)
    voice_names = voice_names or set()

    # Pre-compile the "Eoin & X" / "Eoin <> X" / "X | Eoin" title shape — strong
    # signal that the event is a 1-on-1 whose title literally names both
    # parties. We over-match on purpose; the score is additive, not exclusive.
    eoin_pat = re.compile(r"\beoin\b", re.I)

    candidates = []
    for e in cal_events:
        evt_dt = e.get("_dt")
        if not evt_dt:
            continue
        diff = (rec_dublin - evt_dt).total_seconds()
        # Window: 30 min before event start → 60 min after event END (or
        # +60 min after start if no end time captured). This catches
        # recordings made late in long meetings (e.g. board meeting that
        # overruns by 30 min, recording starts 80 min into it).
        evt_end = e.get("_end_dt")
        if evt_end:
            diff_end = (rec_dublin - evt_end).total_seconds()
            if not (-1800 <= diff and diff_end <= 3600):
                continue
        else:
            if not (-1800 <= diff <= 3600):
                continue

        # ── Score the candidate ────────────────────────────────────────────
        # Negative score is preferred (sorted ascending) so we use minutes-
        # away as the base cost. Bonuses subtract from the cost; penalties add.
        score = abs(diff) / 60.0  # cost in minutes

        title = (e.get("TITLE") or "").lower()
        attendees_str = e.get("ATTENDEES") or ""
        n_attendees = len([a for a in attendees_str.split("|")
                           if a.strip() and a.strip() != "<>"])

        # Title-specificity bonus: title literally names attendees ("Alex & Eoin",
        # "eoin <> declan", "Eoin DCC Catch up"). Eoin's name in the title is
        # a strong "this is the meeting" signal vs generic block titles.
        if eoin_pat.search(title):
            score -= 30
        # "Catch up" / "Catch-up" / "Catchup" titles are usually 1-on-1s
        if re.search(r"\bcatch[\s-]?up\b", title, re.I):
            score -= 15
        # Names like "X & Y" / "X <> Y" — both-parties-in-title pattern
        if re.search(r"\s(?:&|and|<>|vs)\s", title, re.I) or "/" in title:
            score -= 20

        # Attendee count bonus: when Eoin is solo-recording, smaller meetings
        # are usually the actual recorded one. 2-3 attendees is the sweet spot.
        if n_attendees == 0:
            score += 5  # untrustworthy event with no attendees
        elif n_attendees <= 3:
            score -= 25
        elif n_attendees <= 6:
            score -= 5
        else:
            score += (n_attendees - 6) * 2  # bigger meetings rank lower

        # Time penalty: events significantly displaced from recording time
        # are progressively less likely (the linear cost above isn't enough)
        if abs(diff) > 1800:
            score += 10

        # Voice-overlap bonus: dominates timestamp scoring when voices in the
        # room match an event's invitees. Two overlapping events at similar
        # times are otherwise indistinguishable — voice IDs are the only signal
        # that disambiguates them. Scaling: -40 per matched name (compounds for
        # 3-5 person meetings; a single match isn't enough to flip a strong
        # timestamp winner, but ≥2 matches will). Compares last-token (surname)
        # to handle "Khizer" vs "Khizer Ahmed Biyabani" form differences.
        if voice_names:
            evt_attendees = set()
            for a in attendees_str.split("|"):
                a = a.strip()
                if not a or a == "<>":
                    continue
                evt_attendees.add(a)
            # Match by full name OR by last token, case-insensitive
            evt_lower = {a.lower() for a in evt_attendees}
            evt_lasts = {a.split()[-1].lower() for a in evt_attendees if a.split()}
            matches = 0
            for v in voice_names:
                vl = v.lower()
                vlast = v.split()[-1].lower() if v.split() else ""
                if vl in evt_lower or (vlast and vlast in evt_lasts):
                    matches += 1
            if matches:
                score -= 40 * matches

        # We want lowest score = best, and ties broken by absolute time delta
        candidates.append((score, abs(diff), e))

    # Sort ascending: lowest score first, then closest in time
    candidates.sort(key=lambda x: (x[0], x[1]))

    # Same-day voice fallback: if voice evidence covers an event's invitees
    # and that event sits OUTSIDE the standard window, voice wins anyway.
    # Real case: a 9am meeting got rescheduled to 14:00 but the calendar
    # invite stayed at 9am — recording at 14:02 has no in-window event that
    # matches the voices in the room. Trigger only when no in-window
    # candidate matches the voices. Threshold for "matches": full coverage
    # (every confirmed non-Eoin voice maps to an invitee) OR ≥2 matches.
    # That keeps 1-on-1s (1 voice = 1 match) eligible while requiring
    # stronger signal for larger meetings.
    def _voice_stats(evt):
        """Returns (matches, non_eoin_invitee_count, coverage_ratio).
        Coverage = matches / non_eoin_invitees — measures how completely the
        recording's voices fill this event. A small meeting with 1 voice
        match (e.g. Cathal in a Cathal+Eoin 1-on-1) scores 100%; a 19-person
        Governance Board with the same 1 voice match scores ~5%."""
        if not voice_names:
            return 0, 0, 0.0
        attendees_str = evt.get("ATTENDEES") or ""
        attendees = [a.strip() for a in attendees_str.split("|") if a.strip() and a.strip() != "<>"]
        non_eoin = [a for a in attendees if a.lower() not in ("eoin lane", "eoin.lane@adaptcentre.ie", "eoinlane@gmail.com")]
        evt_lower = {a.lower() for a in non_eoin}
        evt_lasts = {a.split()[-1].lower() for a in non_eoin if a.split()}
        matches = sum(1 for v in voice_names
                      if v.lower() in evt_lower
                      or (v.split() and v.split()[-1].lower() in evt_lasts))
        n = max(1, len(non_eoin))
        return matches, len(non_eoin), matches / n

    if voice_names:
        # Best in-window voice coverage
        in_window_best = max(((_voice_stats(e)[2], _voice_stats(e)[0])
                              for _, _, e in candidates), default=(0.0, 0))
        # Search same-day for a stronger coverage match — required when the
        # in-window winner only catches voices incidentally (e.g. Cathal in a
        # 19-person board meeting hides a real 1-on-1 with Cathal that the
        # calendar invite still has at 9am).
        same_day = []
        for e in cal_events:
            evt_dt = e.get("_dt")
            if not evt_dt or evt_dt.date() != rec_dublin.date():
                continue
            matches, n_invitees, coverage = _voice_stats(e)
            if matches == 0:
                continue
            same_day.append((matches, coverage, abs((rec_dublin - evt_dt).total_seconds()), e))
        if same_day:
            # Prefer: highest coverage, then most matches, then closest in time
            same_day.sort(key=lambda x: (-x[1], -x[0], x[2]))
            top_matches, top_cov, top_delta, top_evt = same_day[0]
            # Trigger override if the best same-day candidate beats every
            # in-window candidate by ≥0.4 coverage AND is at least 80% covered.
            # This protects against weak coincidental voice matches winning,
            # while reliably catching rescheduled / out-of-window meetings.
            if top_cov >= 0.8 and top_cov - in_window_best[0] >= 0.4:
                synthetic_score = -100 * top_matches - int(top_cov * 50)
                candidates.insert(0, (synthetic_score, top_delta, top_evt))

    # Return in the legacy 3-tuple shape expected by callers, with the
    # canonical match first.
    return [(time_delta, score, evt) for score, time_delta, evt in candidates[:5]]


def find_meetings(note):
    note_tok = tokens(note["key_people"] + " " + note["summary"]) - NOISE
    candidates = []
    for e in unique_events:
        if not e["_date"]:
            continue
        diff = abs((note["_date"] - e["_date"]).days)
        if diff > 1:
            continue
        mt = mtg_tokens(e)
        overlap = note_tok & mt
        score = len(overlap) * 2 + (2 if diff == 0 else 0)
        if score >= 3:
            candidates.append((score, diff, e))
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[:3]

# ── Build people index ────────────────────────────────────────────────────────
people_index = defaultdict(list)  # canonical_name → list of note filenames

def extract_people_from_attendees(attendee_str):
    people = []
    for a in attendee_str.split("|"):
        a = a.strip()
        if not a or a == "<>":
            continue
        m = re.match(r"^(.*?)<([^>]+)>$", a)
        if m:
            name = m.group(1).strip().strip('"')
            email = m.group(2).strip()
            if name and len(name) > 2:
                people.append((name, email))
            elif email:
                people.append((email, email))
        elif len(a) > 2:
            people.append((a, ""))
    return people

# ── Generate markdown files ───────────────────────────────────────────────────
print("Generating markdown files...")
generated = 0
skipped = 0

all_note_metadata = []  # for index file

for note in notes:
    # Strip any audio/text extension to get the bare stem
    _fn = note["filename"]
    filename_base = re.sub(r'\.(txt|m4a|mp3)$', '', _fn)
    transcript_name = filename_base + ".txt"
    transcript_path = os.path.join(NOTES_DIR, transcript_name)

    # Load transcript
    try:
        raw = icloud_read(transcript_path, errors="replace")
    except FileNotFoundError:
        skipped += 1
        continue

    # Parse recording date/time from transcript header
    recording_dt = None
    rec_match = re.search(r"Recorded:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", raw)
    if rec_match:
        try:
            recording_dt = datetime.strptime(rec_match.group(1), "%Y-%m-%d %H:%M:%S")
        except:
            pass

    # For Plaud recordings: filename IS the recording timestamp (YYYY-MM-DD_HH_MM_SS)
    # Override the transcript header (which may reflect transcription time, not recording time)
    plaud_match = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})_(\d{2})", filename_base)
    if plaud_match:
        try:
            plaud_dt = datetime.strptime(
                f"{plaud_match.group(1)} {plaud_match.group(2)}:{plaud_match.group(3)}:{plaud_match.group(4)}",
                "%Y-%m-%d %H:%M:%S"
            )
            recording_dt = plaud_dt
            # Also override CSV date with the Plaud filename date
            note["_date"] = plaud_dt.date()
        except:
            pass

    # Strip the file header from transcript body
    transcript_body = re.sub(r"^File:.*?\n-{20,}\n", "", raw, flags=re.DOTALL).strip()

    # Find matching calendar meetings — timestamp first, then token fallback.
    # Pass confirmed voice IDs so the matcher can disambiguate overlapping events.
    voice_names = confirmed_voice_names(note["filename"])
    meetings = find_meetings_by_time(recording_dt, unique_events, voice_names=voice_names)
    if not meetings:
        meetings = find_meetings(note)

    # Load LLM-extracted insights if available, else fall back to regex heuristics
    insights = None
    insights_file = os.path.join("/tmp/kb_insights", filename_base + ".json")
    if os.path.exists(insights_file):
        try:
            with open(insights_file) as inf:
                insights = json.load(inf)
            if insights.get("skipped"):
                insights = None
        except (json.JSONDecodeError, KeyError):
            insights = None

    if insights:
        action_items = []
        for ai in insights.get("action_items", []):
            if isinstance(ai, dict) and ai.get("action"):
                owner = ai.get("owner", "")
                action = ai.get("action", "")
                deadline = ai.get("deadline")
                item = f"{owner}: {action}" if owner else action
                if deadline:
                    item += f" (by {deadline})"
                action_items.append(item)
            elif isinstance(ai, str):
                action_items.append(ai)
    else:
        action_items = extract_action_items(transcript_body)

    # Build people list from note + calendar attendees.
    # ONLY use the canonical (best-scored) calendar match — not the union of
    # all overlapping events. Unioning produced polluted attendee lists when
    # multiple events overlapped the recording window, leading downstream
    # speaker-ID and insights to inherit wrong attribution. Near-misses are
    # still listed in the markdown body's "Calendar Meetings" section for
    # transparency, but they don't contribute to `attendees:` frontmatter.
    # Normalise WhisperX mishearings in CSV key_people. Two recurring issues:
    # 1) Eoin Lane (the recorder, in every recording) gets misheard as Owen
    #    Lane / Eoghan Lane / Owen Layne / etc.
    # 2) Cathal Bellew (NTA team member, in most NTA recordings) gets misheard
    #    as Cahal / Carla / Cahill / Cottle / Karl Bellew (with or without 's').
    # The LLM prompts try to enforce normalisation but slip; this is a
    # belt-and-braces fix at the KB layer so frontmatter stays clean.
    EOIN_VARIANTS = ("owen lane", "eoghan lane", "owen layne", "eoin layne", "eoghan layne")
    CATHAL_VARIANTS = ("cathal", "cahal", "cathal murphy", "carla", "cahill", "cottle",
                       "karl bellew", "karl bellews")
    def _normalise(name, category):
        if not name:
            return name
        # Strip parenthetical clarifications: "Owen Lane (Eoin)" → "Owen Lane"
        cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()
        low = cleaned.lower()
        if low in EOIN_VARIANTS:
            return "Eoin Lane"
        if category == "NTA" and low in CATHAL_VARIANTS:
            return "Cathal Bellew"
        return cleaned

    # Split key_people on both `;` and `,` — newer CSV rows use comma-separated
    # names within the field (e.g. "Eoghan Lane, Aoife") while older rows used
    # semicolons. Without splitting on both, a multi-person field gets treated
    # as one stringy "name".
    key_people_raw = re.split(r"[;,]", note["key_people"])
    note_people = [_normalise(p.strip(), note.get("category"))
                   for p in key_people_raw if p.strip()]
    # De-dupe while preserving order — normalisation can collapse multiple
    # variants into the same canonical name.
    seen = set()
    note_people = [x for x in note_people if x and not (x in seen or seen.add(x))]
    all_attendees = []  # (name, email)
    canonical_match = meetings[0] if meetings else None
    if canonical_match:
        _, _, mtg = canonical_match
        for a in mtg.get("ATTENDEES", "").split("|"):
            a = a.strip()
            if not a or a == "<>":
                continue
            m = re.match(r"^(.*?)<([^>]+)>$", a)
            if m:
                name = m.group(1).strip().strip('"')
                email = m.group(2).strip()
                if name and "room" not in name.lower() and "@resource" not in email:
                    all_attendees.append((name, email))
            else:
                # Plain display name (no email) — from Apple Calendar export
                name = a.strip('"')
                if name and "room" not in name.lower():
                    if "@" in name:
                        all_attendees.append(("", name))
                    else:
                        all_attendees.append((name, ""))

    # Deduplicate attendees by email or name
    seen_keys = set()
    unique_attendees = []
    for name, email in all_attendees:
        key = email.lower() if email else name.lower()
        skip = (
            key in seen_keys
            or "eoin" in name.lower()
            or "eoin" in email.lower()
            or "novalconsultancy" in email.lower()
            or "london-" in name.lower()  # conference room
            or "[" in name  # e.g. "[Google Meet]"
        )
        if not skip and (name or email):
            seen_keys.add(key)
            unique_attendees.append((name, email))

    # Build attendee full names list (for frontmatter)
    attendee_names = []
    for name, email in unique_attendees:
        if name and len(name) > 2:
            attendee_names.append(name)
        elif email:
            attendee_names.append(email)

    # Build mentioned list: CSV key_people minus anyone already in attendees
    attendee_names_lower = {n.lower() for n in attendee_names}
    mentioned_names = []
    for p in note_people:
        # Check if this person (possibly first-name only) matches any attendee
        p_lower = p.lower().strip()
        # Skip Eoin (the recorder) and Owen (mishearing of Eoin)
        if p_lower in ("eoin lane", "eoin", "owen lane", "owen"):
            continue
        already_attendee = False
        for att_name in attendee_names:
            att_lower = att_name.lower()
            # Match if key_people name is a substring of attendee name (first name match)
            if p_lower in att_lower or att_lower.startswith(p_lower):
                already_attendee = True
                break
        if not already_attendee and p_lower not in attendee_names_lower:
            mentioned_names.append(p)

    # Calendar-based category override: if all known attendees unanimously map to one
    # category AND the LLM gave a generic category (other:*), use the attendee signal.
    # Don't override specific org categories (NTA, DCC, Paradigm etc.) — the LLM
    # classification from transcript content is more reliable than calendar overlap.
    if attendee_names and note["category"].startswith("other:"):
        attendee_categories = set()
        for name in attendee_names:
            cat = PERSON_CATEGORY.get(name)
            if cat:
                attendee_categories.add(cat)
        if len(attendee_categories) == 1:
            note["category"] = attendee_categories.pop()

    # Build output filename: date_category_slug.md
    date_str = note["_date"].strftime("%Y-%m-%d")
    time_str = recording_dt.strftime("%H%M") if recording_dt else "0000"
    category = note["category"].replace(":", "_")
    topic_slug = slugify(note.get("topic", "note") or "note")
    out_filename = f"{date_str}_{time_str}_{category}_{topic_slug}.md"
    out_path = os.path.join(OUTPUT_DIR, "meetings", out_filename)

    # Update people index
    for name, email in unique_attendees:
        people_index[name].append(out_filename)

    # ── Write markdown ─────────────────────────────────────────────────────
    lines = []

    # YAML frontmatter
    lines.append("---")
    lines.append(f'title: "{note.get("topic") or note["summary"][:60]}"')
    lines.append(f'date: {date_str}')
    if recording_dt:
        lines.append(f'recorded: "{recording_dt.strftime("%Y-%m-%d %H:%M")}"')
    lines.append(f'category: {note["category"]}')
    lines.append(f'topic: "{note.get("topic", "")}"')
    if attendee_names:
        att_yaml = ", ".join(f'"{a}"' for a in attendee_names)
        lines.append(f'attendees: [{att_yaml}]')
    if mentioned_names:
        men_yaml = ", ".join(f'"{m}"' for m in mentioned_names)
        lines.append(f'mentioned: [{men_yaml}]')
    # Legacy people field for backward compat (union of attendees + mentioned)
    all_people_names = attendee_names + mentioned_names if (attendee_names or mentioned_names) else note_people
    people_yaml = ", ".join(f'"{p}"' for p in all_people_names) if all_people_names else '""'
    lines.append(f'people: [{people_yaml}]')

    # Audit trail: who/when/why this meeting got the attendees it did.
    # Lets a reviewer (human or future agent) immediately see whether the
    # attendee list came from a confident calendar match, a CSV fallback,
    # or a manual override — and which calendar event was matched.
    if canonical_match:
        delta_sec, score, mtg = canonical_match
        delta_min = int(round(delta_sec / 60))
        evt_title = (mtg.get("TITLE") or "").replace('"', '\\"')
        lines.append(f'matched_event: "{evt_title}"')
        lines.append(f'matched_event_score: {round(-score, 1)}')  # higher = better, hence negate
        lines.append(f'matched_event_delta_min: {delta_min}')
        lines.append(f'attendees_source: "calendar"')
    elif note_people:
        lines.append(f'attendees_source: "csv:key_people"')
    else:
        lines.append(f'attendees_source: "none"')
    lines.append(f'matched_at: "{datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}"')
    if meetings:
        mtg_titles = ", ".join(f'"{m[2]["TITLE"]}"' for m in meetings)
        lines.append(f'meetings: [{mtg_titles}]')
    lines.append(f'source_file: {note["filename"]}')
    lines.append("---")
    lines.append("")

    # Title
    title = note.get("topic") or note["summary"][:80]
    lines.append(f"# {title}")
    lines.append("")

    # Metadata block
    lines.append("## Overview")
    lines.append("")
    lines.append(f"| Field | Value |")
    lines.append(f"|-------|-------|")
    lines.append(f"| **Date** | {date_str} |")
    if recording_dt:
        lines.append(f'| **Recorded** | {recording_dt.strftime("%Y-%m-%d %H:%M")} |')
    lines.append(f"| **Category** | {note['category']} |")
    lines.append(f"| **Topic** | {note.get('topic', '')} |")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append(note["summary"])
    lines.append("")

    # Calendar meetings
    if meetings:
        lines.append("## Calendar Meetings")
        lines.append("")
        for score, diff, mtg in meetings:
            lines.append(f"### {mtg['TITLE']}")
            if mtg.get("_dt"):
                lines.append(f"**Time:** {mtg['_dt'].strftime('%Y-%m-%d %H:%M')}  ")
            if mtg.get("LOCATION") and mtg["LOCATION"] not in ("missing value", ""):
                lines.append(f"**Location:** {mtg['LOCATION']}  ")
            lines.append("")
            att_str = mtg.get("ATTENDEES", "")
            if att_str:
                lines.append("**Attendees:**")
                lines.append("")
                formatted = format_attendees_md(att_str)
                if formatted:
                    lines.append(formatted)
            lines.append("")

    # People (from notes, without calendar match)
    elif note_people:
        lines.append("## People")
        lines.append("")
        for p in note_people:
            lines.append(f"- {p}")
        lines.append("")

    # Action items
    if action_items:
        lines.append("## Action Items")
        lines.append("")
        for item in action_items:
            lines.append(f"- [ ] {item}")
        lines.append("")

    # LLM-extracted insights (decisions, follow-ups, open questions, key topics)
    if insights:
        decisions = insights.get("decisions", [])
        if decisions:
            lines.append("## Decisions")
            lines.append("")
            for d in decisions:
                lines.append(f"- {d}" if isinstance(d, str) else f"- {d}")
            lines.append("")

        follow_ups = insights.get("follow_ups", [])
        if follow_ups:
            lines.append("## Follow-ups")
            lines.append("")
            for fu in follow_ups:
                if isinstance(fu, dict):
                    desc = fu.get("description", str(fu))
                    who = f" ({fu['who']})" if fu.get("who") else ""
                    lines.append(f"- {desc}{who}")
                else:
                    lines.append(f"- {fu}")
            lines.append("")

        open_qs = insights.get("open_questions", [])
        if open_qs:
            lines.append("## Open Questions")
            lines.append("")
            for q in open_qs:
                lines.append(f"- {q}" if isinstance(q, str) else f"- {q}")
            lines.append("")

        key_topics = insights.get("key_topics", [])
        if key_topics:
            lines.append("## Key Topics")
            lines.append("")
            for t in key_topics:
                lines.append(f"- {t}")
            lines.append("")

    # Full transcript
    lines.append("## Full Transcript")
    lines.append("")
    lines.append("```")
    lines.append(transcript_body)
    lines.append("```")
    lines.append("")

    content = "\n".join(lines)
    if not os.path.exists(out_path) or Path(out_path).read_text() != content:
        with open(out_path, "w") as f:
            f.write(content)

    all_note_metadata.append({
        "file": out_filename,
        "date": date_str,
        "category": note["category"],
        "topic": note.get("topic", ""),
        "summary": note["summary"],
        "people": all_people_names,
        "has_meetings": len(meetings) > 0,
        "attendee_count": len(unique_attendees),
    })

    generated += 1
    if generated % 50 == 0:
        print(f"  Generated {generated}...")

print(f"\nGenerated {generated} markdown files ({skipped} skipped)")

# ── Orphan cleanup ────────────────────────────────────────────────────────────
# Filenames embed the meeting's category and slug, both of which can change
# between builds (e.g. when the LLM re-classifies a recording from "DCC" to
# "ADAPT", or the topic gets normalised). The build emits a new file under
# the new name without removing the old one, leaving stale duplicates that
# pollute search and `mentioned:` fields. Detect duplicates by source_file
# UUID and keep only the most recent (newest matched_at, falling back to
# mtime). One-off cleanup of 178 orphans was needed in 2026-04-28; this
# block prevents them re-accumulating.
print("Cleaning up orphan KB files...")
import collections as _coll
_kb_meetings = os.path.join(OUTPUT_DIR, "meetings")
_by_uuid = _coll.defaultdict(list)
for _f in os.listdir(_kb_meetings):
    if not _f.endswith(".md"):
        continue
    _path = os.path.join(_kb_meetings, _f)
    try:
        with open(_path, errors="replace") as _fh:
            _text = _fh.read(4096)  # frontmatter only
    except OSError:
        continue
    _src_m = re.search(r"^source_file:\s*(\S+)", _text, re.MULTILINE)
    _ma_m = re.search(r'^matched_at:\s*"([^"]+)"', _text, re.MULTILINE)
    if not _src_m:
        continue
    _by_uuid[_src_m.group(1)].append((
        _ma_m.group(1) if _ma_m else "",
        os.path.getmtime(_path),
        _path,
    ))
_removed = 0
for _uuid, _entries in _by_uuid.items():
    if len(_entries) <= 1:
        continue
    # Newer matched_at first; fall back to mtime when timestamps tie or are absent
    _entries.sort(key=lambda x: (x[0], x[1]), reverse=True)
    for _ma, _mt, _path in _entries[1:]:
        try:
            os.remove(_path)
            _removed += 1
        except OSError:
            pass
if _removed:
    print(f"  Removed {_removed} orphan(s)")

# ── Generate people index pages ───────────────────────────────────────────────
print("Building people index pages...")

# Build full people → email map from calendar data
person_emails = {}
for e in unique_events:
    for a in e.get("ATTENDEES", "").split("|"):
        a = a.strip()
        if not a or a == "<>":
            continue
        m = re.match(r"^(.*?)<([^>]+)>$", a)
        if m:
            name = m.group(1).strip().strip('"')
            email = m.group(2).strip()
            if name and len(name) > 2 and "@" in email:
                person_emails[name] = email

for person_name, note_files in sorted(people_index.items()):
    if "eoin" in person_name.lower() or len(person_name) < 3:
        continue
    slug = slugify(person_name)
    out_path = os.path.join(OUTPUT_DIR, "people", f"{slug}.md")
    email = person_emails.get(person_name, "")

    lines = []
    lines.append("---")
    lines.append(f'name: "{person_name}"')
    if email:
        lines.append(f'email: "{email}"')
    lines.append(f'meeting_count: {len(set(note_files))}')
    lines.append("---")
    lines.append("")
    lines.append(f"# {person_name}")
    lines.append("")
    if email:
        lines.append(f"**Email:** {email}  ")
    lines.append(f"**Meetings recorded:** {len(set(note_files))}")
    lines.append("")
    lines.append("## Meetings")
    lines.append("")
    for nf in sorted(set(note_files)):
        lines.append(f"- [[meetings/{nf}]]")
    lines.append("")

    content = "\n".join(lines)
    if not os.path.exists(out_path) or Path(out_path).read_text() != content:
        with open(out_path, "w") as f:
            f.write(content)

print(f"  Built {len(people_index)} people pages")

# ── Generate category index pages ────────────────────────────────────────────
print("Building category index pages...")
from collections import defaultdict
by_category = defaultdict(list)
for m in all_note_metadata:
    by_category[m["category"]].append(m)

topics_dir = os.path.join(OUTPUT_DIR, "topics")
for category, items in sorted(by_category.items()):
    items_sorted = sorted(items, key=lambda x: x["date"])
    slug = slugify(category)
    out_path = os.path.join(topics_dir, f"{slug}.md")
    lines = []
    lines.append("---")
    lines.append(f'category: "{category}"')
    lines.append(f'count: {len(items)}')
    lines.append("---")
    lines.append("")
    lines.append(f"# {category}")
    lines.append("")
    lines.append(f"**{len(items)} recorded meetings/notes**")
    lines.append("")
    lines.append("| Date | Topic | People | Has Calendar |")
    lines.append("|------|-------|--------|--------------|")
    for item in items_sorted:
        ppl = ", ".join(item["people"][:3])
        if len(item["people"]) > 3:
            ppl += f" +{len(item['people'])-3}"
        has_cal = "✓" if item["has_meetings"] else "–"
        topic = (item["topic"] or item["summary"][:50]).replace("|", "\\|")
        lines.append(f"| {item['date']} | [[meetings/{item['file']}\\|{topic}]] | {ppl} | {has_cal} |")
    lines.append("")
    content = "\n".join(lines)
    if not os.path.exists(out_path) or Path(out_path).read_text() != content:
        with open(out_path, "w") as f:
            f.write(content)

print(f"  Built {len(by_category)} category pages")

# ── Generate main index ───────────────────────────────────────────────────────
print("Building main index...")
total_people = len([p for p in people_index if "eoin" not in p.lower() and len(p) > 2])
matched = sum(1 for m in all_note_metadata if m["has_meetings"])

index_lines = []
index_lines.append("# Knowledge Base")
index_lines.append("")
index_lines.append(f"**{len(all_note_metadata)}** meeting notes | "
                   f"**{matched}** matched to calendar | "
                   f"**{total_people}** people")
index_lines.append("")
index_lines.append("## By Category")
index_lines.append("")
for cat, items in sorted(by_category.items(), key=lambda x: -len(x[1])):
    slug = slugify(cat)
    index_lines.append(f"- [[topics/{slug}|{cat}]] ({len(items)} notes)")
index_lines.append("")
index_lines.append("## People")
index_lines.append("")
# Group by org
nta_people = [(n, e) for n, e in sorted(person_emails.items()) if "nationaltransport" in e]
tcd_people = [(n, e) for n, e in sorted(person_emails.items()) if "tcd.ie" in e or "learnovate" in e]
dcc_people = [(n, e) for n, e in sorted(person_emails.items()) if "dublincity" in e or "adaptcentre" in e]
other_people = [(n, e) for n, e in sorted(person_emails.items())
                if not any(d in e for d in ["nationaltransport", "tcd.ie", "learnovate", "dublincity", "adaptcentre", "eoin"])]

for group_name, group in [("NTA", nta_people), ("TCD / Diotima / Learnovate", tcd_people),
                            ("DCC / ADAPT", dcc_people), ("Other", other_people)]:
    if group:
        index_lines.append(f"### {group_name}")
        index_lines.append("")
        for name, email in group[:30]:
            if "eoin" in name.lower():
                continue
            slug = slugify(name)
            index_lines.append(f"- [[people/{slug}|{name}]] ({email})")
        index_lines.append("")

index_lines.append("## Recent Notes")
index_lines.append("")
recent = sorted(all_note_metadata, key=lambda x: x["date"], reverse=True)[:20]
for m in recent:
    index_lines.append(f"- {m['date']} — [[meetings/{m['file']}|{(m['topic'] or m['summary'])[:60]}]]")
index_lines.append("")

with open(os.path.join(OUTPUT_DIR, "README.md"), "w") as f:
    f.write("\n".join(index_lines))

print("\nDone! Knowledge base built at ~/knowledge_base/")
print(f"  meetings/  — {generated} files")
print(f"  people/    — {len(people_index)} files")
print(f"  topics/    — {len(by_category)} files")
print(f"  README.md  — main index")
