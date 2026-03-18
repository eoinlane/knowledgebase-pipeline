"""
Build a markdown knowledge base from meeting notes, transcripts, and calendar data.
Output: ~/knowledge_base/ — one .md file per note, plus index files.
"""

import csv, io, re, os, subprocess, time
from datetime import datetime
from collections import defaultdict


def icloud_read(path, retries=10, delay=30, **kwargs):
    """Read an iCloud file safely despite iCloud's file locking (EDEADLK, errno 11).

    Python's open() and shutil.copy2 both trigger EDEADLK when iCloud's daemon
    holds the file during sync — even binary reads fail. Instead, use the shell
    'cp' command which uses read(2)/write(2) syscalls directly, bypassing fcopyfile.
    Retries with a long delay (30s) since iCloud can hold locks for several minutes.
    """
    tmp_path = f"/tmp/_icloud_{os.getpid()}_{os.path.basename(path)}"
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ['cp', str(path), tmp_path],
                capture_output=True, timeout=30
            )
            if result.returncode != 0:
                raise OSError(f"cp failed: {result.stderr.decode().strip()}")
            try:
                with open(tmp_path, **kwargs) as f:
                    return f.read()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except (OSError, subprocess.TimeoutExpired) as e:
            if attempt < retries - 1:
                time.sleep(delay)
                continue
            raise


def icloud_open(path, retries=5, delay=3, **kwargs):
    """Open an iCloud file with retries on EDEADLK (errno 11)."""
    for attempt in range(retries):
        try:
            return open(path, **kwargs)
        except OSError as e:
            if e.errno == 11 and attempt < retries - 1:
                time.sleep(delay)
                continue
            raise

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
print("Loading calendar data...")
all_events = []
for path in [
    "/tmp/cal_eoinlane.txt",
    "/tmp/cal_work.txt",
    "/tmp/calendar_events.txt",
    "/tmp/cal_extra_15.txt",
    "/tmp/cal_nta.txt",
    "/tmp/cal_personal.txt",
    "/tmp/cal_home.txt",
]:
    all_events += load_cal_file(path)

# Deduplicate
seen_keys = set()
unique_events = []
for e in all_events:
    key = (e.get("TITLE", "").strip().lower()[:40], e.get("START", "")[:16])
    if key not in seen_keys:
        seen_keys.add(key)
        unique_events.append(e)
print(f"  {len(unique_events)} unique calendar events")

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
    filename_base = note["filename"].replace(".txt", "")
    transcript_name = note["filename"] if note["filename"].endswith(".txt") else note["filename"] + ".txt"
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

    # Strip the file header from transcript body
    transcript_body = re.sub(r"^File:.*?\n-{20,}\n", "", raw, flags=re.DOTALL).strip()

    # Find matching calendar meetings
    meetings = find_meetings(note)

    # Extract action items
    action_items = extract_action_items(transcript_body)

    # Build people list from note + calendar attendees
    note_people = [p.strip() for p in note["key_people"].split(";") if p.strip()]
    all_attendees = []  # (name, email, source)
    for _, _, mtg in meetings:
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
                if name and "room" not in name.lower() and "@" not in name:
                    all_attendees.append((name, ""))

    # Deduplicate attendees by email
    seen_emails = set()
    unique_attendees = []
    for name, email in all_attendees:
        key = email.lower() if email else name.lower()
        if key not in seen_emails and "eoin" not in name.lower():
            seen_emails.add(key)
            unique_attendees.append((name, email))

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
    people_yaml = ", ".join(f'"{p}"' for p in note_people) if note_people else '""'
    lines.append(f'people: [{people_yaml}]')
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

    # Full transcript
    lines.append("## Full Transcript")
    lines.append("")
    lines.append("```")
    lines.append(transcript_body)
    lines.append("```")
    lines.append("")

    content = "\n".join(lines)
    if not os.path.exists(out_path) or open(out_path).read() != content:
        with open(out_path, "w") as f:
            f.write(content)

    all_note_metadata.append({
        "file": out_filename,
        "date": date_str,
        "category": note["category"],
        "topic": note.get("topic", ""),
        "summary": note["summary"],
        "people": note_people,
        "has_meetings": len(meetings) > 0,
        "attendee_count": len(unique_attendees),
    })

    generated += 1
    if generated % 50 == 0:
        print(f"  Generated {generated}...")

print(f"\nGenerated {generated} markdown files ({skipped} skipped)")

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
    if not os.path.exists(out_path) or open(out_path).read() != content:
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
    if not os.path.exists(out_path) or open(out_path).read() != content:
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
