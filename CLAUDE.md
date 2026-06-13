# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An automated pipeline that turns iPhone voice memos into a searchable knowledge base. Recordings flow from iPhone → Ubuntu GPU box (transcription + speaker ID) → Mac (KB build → markdown + graph.db). KB is queried via Claude Code + `query_graph.py`. Scripts in this repo run on **both Mac and Ubuntu** — the split matters.

## Running Tests

```bash
# Fast tests only (no SSH, no LLM calls) — run from repo root
pytest tests/ --ignore=tests/test_integration.py

# All tests including Ubuntu SSH + LLM smoke tests
pytest tests/ --run-slow
```

Tests run against **live data** (real CSV, real KB files). They are not isolated — they validate the actual pipeline state on the Mac.

## Key Operations

**Full rebuild (Mac):**
```bash
bash ~/.local/bin/rebuild-knowledge-base.sh
# Or just the KB + contacts + graph:
python3 ~/build_knowledge_base.py
python3 ~/knowledgebase-pipeline/mac/build_contacts_db.py
python3 ~/knowledgebase-pipeline/mac/build_graph.py
```

**Query the knowledge graph:**
```bash
python3 ~/query_graph.py prep "Pat Nestor" -p DCC       # pre-meeting briefing
python3 ~/query_graph.py review                         # weekly digest
python3 ~/query_graph.py review --weeks 2               # last 2 weeks
python3 ~/query_graph.py synthesise "Pat Nestor"        # progressive summary (person, Opus 4.7 default)
python3 ~/query_graph.py synthesise "Pat Nestor" --fast # use Haiku (~20× cheaper, faster, shallower)
python3 ~/query_graph.py synthesise --project NTA       # progressive summary (project, Opus 4.7 default)
python3 ~/query_graph.py open --project DCC             # open action items
python3 ~/query_graph.py open --person "Pat Nestor"     # items for a person
python3 ~/query_graph.py done 42                        # close item by ID
python3 ~/query_graph.py done "send slides"             # close by text match
python3 ~/query_graph.py decisions --project NTA        # decisions by project
python3 ~/query_graph.py history "Jamie Cudden"         # meeting history
python3 ~/query_graph.py stats                          # graph overview
python3 ~/query_graph.py focus                          # curated dry-run for Apple Reminders (add --push to write)
python3 ~/query_graph.py brief                          # daily morning brief (today's meetings + per-attendee last commitment)
python3 ~/query_graph.py stale-nudge                    # Friday weekly: your open items older than 3 weeks
python3 ~/query_graph.py context "Pat Nestor"           # compact context block for outbound email/chat
python3 ~/query_graph.py open --project NTA --by-date   # legacy date-desc ordering (default is priority-bucketed)
```

**Daily morning brief (added 2026-05-22, email added 2026-05-23):**
- `query_graph.py brief` runs the day's brief: today's calendar meetings with per-attendee last open commitment, Eoin's open items from last 2 weeks, items closed in the last 24h, and 2–4 week-old items owed to Eoin. Calendar attendees are cleaned (drops PS4/HR/All-in-NTA/To:X noise, titlecases email-prefix names). Personal + Home calendars excluded at source.
- Launchd agent `com.eoin.morning-brief` fires at 06:30 daily. Writes `~/morning_brief.md` (stable path) + `~/knowledge_base/_briefs/YYYY-MM-DD.md` (archive) + emails to `eoinlane@gmail.com` via Gmail SMTP (app password in macOS keychain, service `morning-brief-smtp`). Sender at `mac/morning_brief_emailer.py` (reusable, takes `--file` + `--subject`).
- **Coverage sections (added 2026-05-30):** brief now also surfaces (a) stuck 0-byte Apple Notes recordings from `~/.local/share/kb/stuck_recordings.txt` (populated by the `sync-notes-audio` escape hatch when an iCloud placeholder sits at 0 bytes for >24h) and (b) calendar events in the last 7 days with no matching transcript within ±90 min (catches Stage A failures: iPhone export shortcut didn't fire, recording forgotten, etc.). Both sections only render when there are entries; brief stays clean when nothing's wrong.

**Weekly stale-commitment nudge (added 2026-05-24):**
- `query_graph.py stale-nudge` surfaces Eoin-owned open commitments older than 3 weeks. Top 3 per project, hard cap 15. Companion to the daily brief — catches things you've quietly dropped.
- Launchd agent `com.eoin.stale-nudge` fires Friday 06:30. Output: `~/stale_nudge.md` (stable) + `~/knowledge_base/_nudges/YYYY-MM-DD.md` (archive) + email via the same sender as the morning brief.

**Close-by-email (added 2026-06-14):**
- Both the daily brief and Friday stale-nudge now render `· [close](mailto:eoinlane+kbclose@gmail.com?subject=close <id>)` beside each Eoin-owned open item. Tapping fires a mailto: with the item id baked into the subject.
- `mac/process_close_replies.py` polls Gmail IMAP every 15 min via launchd `com.eoin.process-close-replies`, fetches `UNSEEN FROM eoinlane@gmail.com SUBJECT close`, parses `close <id>` from the subject, shells out to `query_graph.py done <id>` (idempotent if already closed), and marks SEEN. Cap 50 per run as a runaway guard.
- Auth = same keychain entry (`morning-brief-smtp`/`eoinlane@gmail.com`) — Gmail app passwords cover both SMTP and IMAP. Auth boundary is the `FROM eoinlane@gmail.com` filter; Gmail's DMARC stops same-domain spoofs.
- Closes the open-loop problem: pre-2026-06-14, only 1 of 5,712 action items had ever been explicitly closed (87% auto-marked stale). The brief is now the queue, the inbox is the close button.

**Weekly benchmark + regression alert (added 2026-05-24):**
- `mac/launchd/weekly-benchmark.sh` runs `tools/benchmark_models.py` against the 8-transcript curated suite for `qwen2.5:14b` (Ollama, classify primary) and `claude-haiku-4-5` (LiteLLM, insights primary), then diffs vs the previous run for each model.
- Launchd agent `com.eoin.weekly-benchmark` fires Sunday 02:00 IST (before 04:00 nightly rebuild). Regression rules: exact-accuracy drops OR avg wall +25%. On regression: emails `~/weekly_benchmark_report.md`. Silent on clean.
- Locks in the prompt + model-pinning gains so silent drift gets caught.

**Apple Reminders integration:**
- `query_graph.py focus` curates the focus list; `--push` writes to Apple Reminders via `mac/apple_reminders.py` (osascript helpers — no MCP dependency from the CLI). Lists are `KB:<project>` + `KB:Today`. Dedupes via `[kb-id]` line in each reminder's notes. KB:Today is rolling (stale entries pruned each push); per-project lists are append-only.
- Curation rules: Eoin-owned items, fresh (4-week window), excluded projects (`other:personal`, `FutureBusiness` by default), quality filter (drops weak-verb + summary-boilerplate items), max 3 per project, hard cap 10 total, plus a "Today" cross-cut of top 3 by recording date.
- Write-back (completed reminders → `~/.graph_closures.json`) is the remaining piece. Design captured in memory dossier `project_apple_reminders_integration.md`.

**Outbound email context (added 2026-05-24):**
- `query_graph.py context "Person Name"` outputs a compact markdown block — last meeting date + cadence + the most-recent open commitments both ways + recent decisions from joint meetings. Designed to load before drafting outbound email/chat to that person, so commitments don't get missed and tone stays grounded.

**Contacts web UI:**
```bash
python3 ~/knowledgebase-pipeline/contacts_viewer.py
# Open http://localhost:5100
# Kill existing: kill $(lsof -ti :5100)
# Pages: / (contacts), /meetings (meetings browser), /review (duplicate review)
```

**Re-run speaker ID on a single transcript (Ubuntu):**
```bash
ssh eoin@nvidiaubuntubox
source ~/whisper-env/bin/activate
python3 ~/identify_speakers.py ~/audio-inbox/Transcriptions/<uuid>.txt ~/audio-inbox/classification.csv
```

**Re-classify a timed-out recording (Ubuntu):**
```bash
python3 ~/classify_transcript.py ~/audio-inbox/Transcriptions/<uuid>.txt ~/audio-inbox/classification.csv
```

**Batch speaker ID overnight (Ubuntu):**
```bash
nohup bash -c 'source ~/whisper-env/bin/activate && python3 -u ~/batch_identify_speakers.py' \
  > ~/audio-inbox/speaker_id_batch.log 2>&1 &
```

**Check pipeline health:**
```bash
tail -50 ~/.local/bin/rebuild-knowledge-base.log
ssh eoin@nvidiaubuntubox "systemctl is-active notes-watcher && systemctl --user is-active litellm"
```

## Architecture

### Repository Structure

```
ubuntu/          — scripts that run on Ubuntu GPU box
mac/             — scripts that run on Mac
mac/launchd/     — Mac launchd agent scripts
shared/          — shared config (OLLAMA_URL, MODEL, PERSON_CATEGORY, name expansions)
tools/           — benchmark and dev tools
tests/           — 62 tests (pytest)
docs/            — proposals and documentation
benchmark_results/ — benchmark JSON outputs
Makefile         — deploy-ubuntu, deploy-mac, test, clean-ubuntu
```

**Deployment:** Scripts run via symlinks from `~/` to the repo. `make deploy-ubuntu` rsyncs `ubuntu/` + `shared/` to Ubuntu and creates symlinks. `make deploy-mac` symlinks Mac scripts. Never edit scripts at `~/` directly — edit in the repo, then deploy.

### Mac vs Ubuntu Split

Scripts in **`ubuntu/`** (GPU required): `transcribe_single.py`, `classify_transcript.py`, `identify_speakers.py`, `reclassify_by_speaker.py`, `extract_meeting_insights.py`, `batch_identify_speakers.py`, `review_speakers.py`, `watch-and-transcribe.sh`, `watchdog-transcribe.sh`, `process_audio.sh` (reusable single-file pipeline runner with honest DONE/FAILED markers — replaces ad-hoc `/tmp/process_*.sh` one-liners), `auto_enrol_1on1.py` (cold-start voice catalog enrolment).

Scripts in **`mac/`**: `build_knowledge_base.py`, `build_contacts_db.py`, `build_graph.py`, `query_graph.py`, `contacts_viewer.py`, `apply_kb_corrections.py`, `process_inbox.py`, `upload_knowledge_base_incremental.py`.

**`shared/config.py`** has OLLAMA_URL, MODEL, PERSON_CATEGORY (person→category mapping), KEEP_CATEGORIES, `WHISPER_INITIAL_PROMPT` (decoder name-biasing), `HAIKU_MODEL` / `SONNET_MODEL` / `OPUS_MODEL` (added 2026-05-30 — `claude-opus-4-7`) / `LITELLM_URL[_REMOTE]` (Anthropic via LiteLLM proxy), `OLLAMA_MODEL_DIGEST_EXPECTED` (qwen2.5:14b sha256 for drift detection). **`shared/name_expansions.py`** has WhisperX mishearing→full name tables per category. **`shared/prompts.py`** owns `CLASSIFY_SYSTEM_PROMPT` (single source of truth — imported by both `ubuntu/classify_transcript.py` and `tools/benchmark_models.py`). All `shared/*` modules imported by Ubuntu and Mac scripts with fallback to hardcoded values.

### Data Flow

```
classification.csv (iCloud)  +  Apple Calendar (AppleScript live export + /tmp/cal_*.txt)
    ↓
build_knowledge_base.py  →  ~/knowledge_base/meetings/*.md  (attendees from calendar, mentioned from LLM)
                                             people/*.md    (full names from calendar attendees)
                                             topics/*.md
    ↓
apply_kb_corrections.py  ←  ~/kb_corrections.json (manual overrides)
    ↓
build_contacts_db.py  →  ~/contacts.db  (meetings, people, attendees tables)
                           entity_resolution.py  →  merge_suggestions table
    ↓
build_graph.py  →  ~/graph.db  (action_items, decisions, graph_edges)
                      query_graph.py  →  CLI for pre-meeting briefings, open items
```

### KB Meeting Frontmatter

Meeting files have split people fields:
- `attendees` — full names from canonical calendar event match
- `mentioned` — names from LLM `key_people` minus attendees (people discussed but not present)
- `people` — legacy union of both for backward compatibility
- `matched_event` / `matched_event_score` / `matched_event_delta_min` / `attendees_source` / `matched_at` — audit trail showing which calendar event was matched, the score (lower = better), the delta in minutes from event start, and where attendees came from (`calendar` / `csv:key_people` / `none`). Lets a reviewer immediately see why a meeting got the attendees it did.

### Calendar Matching

`find_meetings_by_time()` in `build_knowledge_base.py` picks ONE canonical event per recording (not the union of overlapping events). Logic in priority order:

1. **Window:** `[event_start − 30 min, event_end + 60 min]` in Dublin local. END comes from icalBuddy export (no `-eed` flag); falls back to `start + 60 min` window when END is missing.
2. **Score (lower = better):** `cost_minutes = |delta|/60` plus title and attendee-count bonuses/penalties:
   - Title contains "eoin": −30
   - "catch up" / "catch-up": −15
   - "X & Y" / "X <> Y" / "X / Y": −20
   - Attendee count ≤ 3: −25; ≤ 6: −5; > 6: progressive penalty
3. **Voice-overlap bonus:** when `~/.local/share/kb/speaker_mappings.json` has confirmed voice IDs for the recording (excluding Eoin), each match against an event's invitees gives a `−40` bonus. ≥ 2 matches reliably flips the choice when timestamps alone are ambiguous (e.g. two overlapping calendar events both within window).
4. **Out-of-window voice fallback:** if no in-window event achieves ≥ 80% voice coverage and a same-day event covers ≥ 80% of confirmed voices with a coverage delta ≥ 0.4 over the best in-window candidate, that out-of-window event wins. Catches rescheduled meetings where the calendar wasn't updated.

Calendar export at `~/.local/bin/export-calendars.sh` writes to `~/.local/share/kb/calendars/cal_*.txt`; survives reboot (formerly `/tmp/`). Last-first names from Outlook ("Dooley, Alan") are recombined into "Alan Dooley" by the export awk.

### Name Normalisation

`build_knowledge_base.py` normalises CSV `key_people` at build time as a belt-and-braces fix when the LLM mishears recurring names:

- **Eoin Lane variants** (always the recorder): `Owen Lane`, `Eoghan Lane`, `Owen Layne` → `Eoin Lane`. Strips parenthetical clarifications first ("Owen Lane (Eoin)" → "Owen Lane" → "Eoin Lane").
- **Cathal Bellew variants** (only in NTA category): `Cathal`, `Cahal`, `Cathal Murphy`, `Carla`, `Cahill`, `Cottle`, `Karl Bellew(s)` → `Cathal Bellew`.
- **Aidan Blighe variants** (DCC category, added 2026-05-30): `aidan bly`, `aidan blie`, `aiden bly`, `aiden blighe` → `Aidan Blighe`. He chairs the DCC AI Governance Group; LLM kept producing `Aidan Bly?`.
- **Two-Shakespeares guard** (added 2026-05-30): `name_expansions.py` has a `# WARNING:` comment forbidding any `richard shakespeare` → `Richie Shakespeare` mapping. There are two Shakespeares at DCC — Richard (Chief Executive, father) and Richie (GenAI Lab member, son). Collapsing them silently re-attributes CE-level directives to a Lab member. See `memory/feedback_two_shakespeares.md`.
- LLM prompts in `classify_transcript.py` and `identify_speakers.py` enforce the same normalisation upstream.

### Orphan KB File Cleanup

When a meeting's category or topic changes between builds, the new file lands at `{date}_{time}_{new_category}_{new_slug}.md` while the old file persists. The build's tail-end orphan-cleanup block groups all `meetings/*.md` by `source_file` UUID and deletes all but the file with the most recent `matched_at` (or mtime). Logs `Removed N orphan(s)` when it acts. A separate launchd `com.eoin.check-kb-orphans` (Mondays 09:17) verifies the cleanup is firing and warns if orphans re-accumulate.

### Corrections Layer

`~/kb_corrections.json` is the **durable override layer** — it survives nightly rebuilds. Structure:
```json
{
  "people": { "RawName": { "name": "Full Name", "title": "...", "org": "..." } },
  "meetings": {
    "filename.md": {
      "topic": "...",
      "tags": [...],
      "people_corrections": { "Old Name": "New Name" },
      "attendees_drop": ["Khizer Ahmed Biyabani", "Yang Su"]
    }
  }
}
```
`apply_kb_corrections.py` patches KB markdown files after each build. `contacts_viewer.py` writes to this file when you edit names/orgs/topics in the UI. Changes propagate on the next rebuild.

**Three field types per meeting:**
- `people_corrections` — rename map; renames in both frontmatter `people` and body text.
- `attendees_drop` (added 2026-05-30) — list of names to strip from `attendees` / `mentioned` / `people` frontmatter without touching body. Use when an invitee didn't actually attend, or when someone has left a project but stays on recurring calendar invites (e.g. Yang Su leaving the GenAI Lab). Distinct from `people_corrections`: drop is a hard remove, applied per-meeting.
- `topic` / `tags` — straightforward frontmatter overrides.

### Contacts DB Schema

```sql
meetings (id, filename, title, date, category, topic, summary, tags)
people   (id, name, slug, primary_org, meeting_count, last_seen, has_file,
          resolved_name, resolved_slug, title, org_detail)
attendees (meeting_id, person_name)
merge_suggestions (canonical_raw, canonical_name, ..., alias_raw, alias_name, ..., reason, confidence, status)
dismissed_pairs (name1, name2)
```

`name` = raw name as it appears in transcript. `resolved_name` = matched full name from `people/*.md` files (via meeting overlap). `display_name` in queries = `COALESCE(resolved_name, name)`.

### Entity Resolution

`entity_resolution.py` detects duplicate people entries. Detection rules (in priority order): exact first-name match against multi-word name (`first_name_only`), word-boundary prefix containment (`name_contained`), Levenshtein distance ≤ 2 on similar-length names (`edit_distance_N`), SequenceMatcher ≥ 0.75 for single-word names (`similar_Npct`). Pairs that ever co-occur in the same meeting are excluded. Score boosted by same org (+0.12), penalised by different known orgs (−0.20).

**LLM judgment layer** (`entity_resolver_agent.py`, added 2026-04-27): for each pending suggestion, gathers context (orgs, meeting counts, top categories, recent meetings, co-attendees) and asks Claude Haiku via the LiteLLM proxy at `http://100.121.184.27:4000` whether the two names refer to the same person. Persists `llm_verdict` (`merge` / `distinct` / `ambiguous`), `llm_confidence`, `llm_reason` on the row. Wired into the nightly rebuild after `build_contacts_db.py` with `--limit 200` so backlog drains gradually; safe to skip if LiteLLM is unreachable.

**Auto-apply layer** (`auto_apply_verdicts.py`, added 2026-06-14): runs after the agent, drains the verdict backlog without waiting on `/review`. Pre-2026-06-14 the backlog grew to 1,031 pending because the human-review step never ran. Conservative safety guards on merges — only auto-applies unambiguous identity patterns:
- email-prefix dedupe (`tom.pollock` ↔ `Tom Pollock`) — only when the other side actually contains both prefix parts, stops `tom.pollock` from getting glued to `Tom Curran`.
- SPEAKER_NN ↔ Speaker_NN transcript labels.
- spelling variant: both multi-word, share a token, edit distance ≤ 2 (catches `Connor` vs `Conor Daly`).
First-name → full-name merges are NOT auto-applied — the contacts DB has 5 Alans, 9 Neils, 2 Shakespeares, 2 Cathals etc., so first-name collapse is almost always ambiguous and gets left for human `/review`. Direction is also normalised: the human-readable form always wins canonical, even if entity_resolution picked the email-prefix as canonical. Distinct-verdict dismissals at confidence ≥ 0.9 are auto-applied (inherently safe — worst case a future genuine match just re-surfaces). First run drained 1,031 → 513 pending (5 safe merges + 513 dismisses).

### Inbox Processing

`process_inbox.py` watches `~/inbox/` (via launchd WatchPaths). Supported: `.pdf`, `.docx`, `.pptx`, `.eml`, `.txt`, `.md`, images. Classification via LiteLLM proxy at `http://100.121.184.27:4000` using `claude-haiku-4-5`. Outputs to `~/knowledge_base/documents/`. Emails (`.eml`) extract body + embedded attachments into a single KB doc with `type: email` frontmatter.

**Audio dropped in `~/inbox/`** is handled by a sibling agent `com.eoin.sync-inbox-audio` (`mac/launchd/sync-inbox-audio.sh`), which rsyncs `.mp3` / `.m4a` to `nvidiaubuntubox:~/audio-inbox/Notes/` and then deletes the local Mac copy — the canonical audio lives on Ubuntu (and Plaud keeps its own copy on the device), so the Mac copy is just disk noise. Plaud recordings (filename `YYYY-MM-DD_HH_MM_SS.mp3`) are then picked up by the Ubuntu `transcribe-watchdog.timer` (every 30 min, `MIN_AGE_MINUTES=15`) — the inotify watcher (`watch-and-transcribe.sh`) only fires on `.m4a`. Finder duplicates (`* copy*`, `*_copy*`) are skipped to avoid re-processing.

**Apple Notes recordings** (UUID-named `.m4a`) come from a separate path: when you record into an Apple Note, an iPhone Shortcut (or equivalent automation) drops the audio into iCloud Drive at `~/Library/Mobile Documents/com~apple~CloudDocs/My Notes Audio/`. The Mac launchd `com.eoin.sync-notes-audio` (`mac/launchd/sync-notes-audio.sh`) runs every 5 min, copies new files to `/tmp/notes-audio-sync/` to avoid iCloud `EDEADLK` mmap locks, then rsyncs to Ubuntu's `~/audio-inbox/Notes/`. Any new `.m4a` there fires the inotify watcher. **Apple Notes ≠ iCloud Drive** — recordings live in the Notes data store until something exports them; if the audio shows in the Notes app but doesn't reach `My Notes Audio/`, the export step hasn't fired yet.

**Stuck-orphan escape hatch (added 2026-05-30):** the sync agent's size-stability check used to silently re-skip 0-byte iCloud placeholders forever (caught 2026-05-27 by the 597B492C orphan that had been stuck since 24 May). The agent now distinguishes "still syncing" from "stuck": if a file has been 0 bytes for `STUCK_AGE_SECS` (24h), it logs a `STUCK` line and appends to `~/.local/share/kb/stuck_recordings.txt` (atomically rewritten each run). The morning brief surfaces this list at 06:30 so the orphan is visible within hours instead of days.

### Knowledge Graph (graph.db)

`build_graph.py` runs after `build_contacts_db.py`. Reads KB frontmatter + insights JSONs (`/tmp/kb_insights/`), outputs `~/graph.db` (SQLite). `query_graph.py` is the CLI query tool.

**Schema:** `action_items` (text, owner, status), `decisions` (text, status), `graph_edges` (from/to type+id, edge_type, confidence), `concepts` (label, category, mention_count — populated from key_topics), `syntheses` (entity_type, entity_id, text — LLM-generated, preserved across rebuilds).

**Edge types:** `SPOKE_IN` (calendar attendee → meeting), `MENTIONED_IN` (person → meeting), `PRODUCED` (meeting → action_item/decision), `ASSIGNED_TO` (action_item → person), `FOLLOW_UP` (person → meeting), `PART_OF` (meeting/document → category), `DISCUSSED` (meeting → concept/tag), `REFERENCED_IN` (person → document).

**People enrichment:** Two sources beyond frontmatter: (1) action item owners + follow-up assignees from insights JSON (confidence 0.9), (2) known-name scanning of transcript body against contacts.db roster (confidence 0.8).

**Owner gating** (added 2026-04-28): every owner pulled from insights JSON is gated through `_gate_owner()` in `build_graph.py`. The gate (a) special-cases Eoin Lane variants → `eoin-lane`, (b) resolves through `shared.entity_resolver`, (c) falls back to matching this meeting's attendees, (d) drops the owner (NULL) if none match. When the slug resolves, the displayed owner is normalised to the canonical name from contacts.db (so "Cathal Murphy"/"Kizzer" become "Cathal Bellew"/"Khizer Ahmed Biyabani"). Surrounding junk chars (`'"\`*[]`) are stripped. On 2026-04-28 this dropped 1374 phantom owners (31% of action items) and consolidated 5 Cathal-spellings into one.

**Entity resolution in graph:** Merges WhisperX mishearings (e.g. `pat-nester` → `pat-nestor`), first-name-only → full names via contacts.db resolved_name (preferring longer name), strips SPEAKER_XX/unknown/compound/junk entries. Resolver built from hardcoded mishearings + contacts.db mappings.

**Insights extraction** (`extract_meeting_insights.py`) now passes CSV `key_people` + category + topic to the LLM as participant context, so action items get real owner names even when transcript has SPEAKER_XX labels.

**Action item lifecycle:** Items older than 8 weeks are auto-marked `stale` during rebuild. Manual closures via `done` command are persisted to `~/.graph_closures.json` (keyed by `meeting_filename::text_prefix`) and survive rebuilds.

**Progressive summarisation:** `synthesise` command calls **Claude Opus 4.7** (default since 2026-05-30) via LiteLLM to produce trajectory narratives per person/project from meeting summaries, action items, and decisions. Use `--fast` to drop to Claude Haiku for ~20× cheaper / ~2× faster but shallower output — appropriate for routine iteration; Opus is appropriate when the deeper read is wanted. Per-synthesis model recorded in the `syntheses` row. `call_haiku()` (name kept for backwards compat) takes optional `model=` and drops `temperature` for Opus-family models (Opus 4.7 deprecated it). Stored in `syntheses` table (preserved across rebuilds). On re-run, previous synthesis is included as context for progressive compression.

**Weekly review:** `review` command shows meetings by project, your commitments, others' commitments, decisions made, overdue items (2-8 weeks), and people gone quiet (3+ weeks). The `com.eoin.weekly-review` launchd agent runs this every Monday 07:00 with `--weeks 2` (covers the week that just ended) and writes to `~/knowledge_base/_reviews/YYYY-Wnn.md`.

**Haiku fallback:** `classify_transcript.py` and `identify_speakers.py` try ollama-box first, fall back to Claude Haiku via LiteLLM if ollama is unreachable. Pipeline keeps running when Proxmox box is offline.

**Tags/concepts:** `key_topics` from insights extraction are normalised and stored in the `concepts` table with `DISCUSSED` edges to meetings. Queryable via `query_graph.py tags` — browse top tags, filter by project, search cross-project.

**Calendar-based category override:** If all calendar attendees map to one category via `PERSON_CATEGORY` and the LLM gave a generic `other:*` category, the attendee signal overrides. Does not override specific org categories.

**Calendar export:** Uses `icalBuddy` (not AppleScript) with stable calendar UIDs. Expands recurring events, runs in ~1 second, no Calendar.app dependency. Config at `~/.local/bin/export-calendars.sh`.

**Design inspiration:** Tiago Forte's "Building a Second Brain" (CODE framework). Pipeline implements Capture (automated), Organise (domain categories), Distil (LLM extraction + progressive summarisation), Express (prep briefings, weekly review, tags). See `docs/` for the book.

### Open WebUI (retired 2026-04-27)

Open WebUI is no longer part of the pipeline. KB queries are handled by Claude Code + `query_graph.py`. The upload step has been removed from both `rebuild-knowledge-base.sh` and `sync-knowledge-base.sh`. `upload_knowledge_base_incremental.py` is left in the repo as legacy and not invoked by any agent.

### iCloud File Access

All reads from `~/Library/Mobile Documents/com~apple~CloudDocs/` go through `icloud_read()` in `build_knowledge_base.py`, which copies to `/tmp` before reading. This sidesteps EDEADLK locking from iCloud's sync daemon. Never attempt direct reads or retries on iCloud paths.

### Speaker Identification

`identify_speakers.py` (Ubuntu) rewrites `[SPEAKER_XX]` labels in transcripts using:
1. Voice matching: cosine similarity of ECAPA-TDNN embeddings against `~/voice_catalog.json` (≥0.80 = high confidence, ≥0.70 = medium)
2. LLM fallback: qwen2.5:14b (via ollama-box) with calendar attendees, name-call cues from transcript, and speech samples from `~/speaker_registry.json`

The name expansion table is in `shared/name_expansions.py` (e.g. DCC: `"kizzer"` → `"Khizer Ahmed Biyabani"`, DFB: `"rob hell"` → `"Rob Howell"`). The script body is wrapped in `if __name__ == "__main__":` so functions are importable for testing.

### Pipeline Steps (Ubuntu watchdog)

```
1. Transcribe (WhisperX large-v3, RTX 5060 Ti) → .txt + voice embeddings
2. Classify (qwen2.5:14b via ollama-box) → category, topic, summary, key_people → CSV
3. Speaker ID → voice catalog match first, LLM fallback for unknowns → rewrite transcript
4. Reclassify by speaker → if voice-identified speakers map to one category, override LLM
5. Extract insights (qwen2.5:14b) → action items, decisions, follow-ups, open questions → JSON
```

### Voice Catalog (Ubuntu ~/voice_catalog.json)

49 people enrolled (as of 2026-06-01) via 2-speaker call elimination (Eoin as anchor), calendar matching, transcript name extraction, and the cold-start auto-enrol script (see below). Grows automatically as new recordings are processed and confirmed. `reclassify_by_speaker.py` uses `shared/config.py:PERSON_CATEGORY` to override LLM categories based on who's speaking.

**Auto-enrolment is two-tier:**
1. **`identify_speakers.py:auto_enrol()`** — extends existing catalog entries: appends the recording's embedding when a voice matches a known person with ≥0.92 similarity. Compounds reliability over time. Runs inline at speaker_id step.
2. **`auto_enrol_1on1.py`** (added 2026-05-30, Ubuntu) — **cold-start**: enrols brand-new people from 1-on-1 calls. For each KB meeting with exactly 2 calendar attendees (Eoin + X) where X isn't yet in the catalog AND the recording has exactly 2 SPEAKER clusters AND one matches Eoin (≥0.65) AND the other has no existing catalog match (<0.55), enrol the unmatched embedding as X. Conservative gating prevents stealing voices that are already in the catalog under a different name. Wired into `mac/launchd/rebuild-knowledge-base.sh` as **Step 4** (after KB rsync to Ubuntu, before memory-symlinks refresh). First run picked up 6 historic 1-on-1s the pipeline had been missing.

**Resilience:** all writes to `voice_catalog.json` and `speaker_mappings.json` go through `shared.atomic_io.atomic_write_json` (write-to-temp + fsync + rename) — eliminates the torn-write / 0-byte file failure mode that would otherwise destroy the catalog if a process gets killed mid-write. Nightly backup launchd `com.eoin.backup-voice-state` rsyncs both files plus `speaker_registry.json` to `~/.local/share/kb/backups/voice/YYYY-MM-DD/` on the Mac (30-day rotation, sanity-checks the catalog parses + has ≥5 people before pruning).

`transcribe_single.py` uses WhisperX `large-v3` with post-processing `dedupe_segments()` to strip hallucinated repeated segments. Also extracts ECAPA-TDNN voice embeddings per speaker. LLM inference (classification + speaker ID + insights) runs on ollama-box (192.168.0.70), completely separate from the Ubuntu transcription GPU.

## Infrastructure

| Component | Details |
|---|---|
| Ubuntu | `eoin@nvidiaubuntubox`, Tailscale `100.121.184.27`, SSH key auth (ed25519 in 1Password SSH agent on the M1 Air; non-interactive launchd ssh falls through gracefully without Touch ID). Sudo password for `eoin` rotated off the weak `el` on 2026-06-13 → stored in 1Password → Homelab → "ollama-box VM" (see [[reference_home_infra_repo]]). MagicDNS is off tailnet-wide (preserves AdGuard filtering); the Mac resolves the hostname via `~/.ssh/config` alias to the Tailscale IP |
| LiteLLM proxy | Ubuntu port 4000, models: `claude-sonnet-4-6`, `claude-haiku-4-5` (pinned to `claude-haiku-4-5-20251001` in `/home/eoin/litellm-config.yaml`), `claude-opus-4-7` (added 2026-05-30). Anthropic API key refactored 2026-05-30: now lives in `/home/eoin/.litellm.env` (mode 600) referenced via `EnvironmentFile` in `~/.config/systemd/user/litellm.service` and consumed as `api_key: os.environ/ANTHROPIC_API_KEY` in each model entry (was hardcoded twice in plaintext). Mac mirror: `security find-generic-password -s anthropic-api-key -a eoin -w`. User `eoin` has `loginctl enable-linger` set since 2026-05-24 — without it, the user-systemd manager died when the last SSH session disconnected and took litellm with it. |
| ollama-box | `192.168.0.70:11434`, Debian 13 bhyve VM on FreeBSD (192.168.0.14), RTX 4060 8GB, `qwen2.5:14b` (~8 tok/s, ~13s/classification). Start VM: `ssh eoin@192.168.0.14 "sudo vm start ollama-box"` — sudo password prompts interactively; fetch from 1Password → Homelab → "FreeBSD root/sudo" (`el` rotated 2026-06-13). |
| WhisperX | Ubuntu RTX 5060 Ti 16GB, model `large-v3`, CUDA float16. `watch-and-transcribe.sh` handles new files via inotify; `watchdog-transcribe.sh` runs every 30 min via systemd timer to catch misses and retry failed classifications |
| WhisperX env | Ubuntu `~/whisper-env/` — always activate before running transcription scripts |
| FreeBSD host | `eoin@192.168.0.14`, Ryzen 5 5600G, 32GB RAM, runs ollama-box bhyve VM. Start VM: `sudo vm start ollama-box` |
| Health check | `~/.local/bin/pipeline-health-check.sh` (Mac, hourly via launchd). Monitors: Ubuntu SSH, FreeBSD SSH, ollama-box API + GPU, services, backlog, watchdog, stale transcriptions |
| Benchmark | `python3 benchmark_models.py --model <name>` — reproducible speed+quality comparison on 8 curated transcripts |

## KB File Conventions

- Meeting filenames: `YYYY-MM-DD_HHMM_CATEGORY_slug.md`
- People filenames: slugified full name (e.g. `cathal-murphy.md`) — Eoin Lane has no people file
- Categories: `NTA`, `DCC`, `DFB`, `ADAPT`, `Diotima`, `Paradigm`, `TBS`, `LCC`, `other:*`
- "Owen Lane" in transcripts = Eoin Lane (WhisperX mishearing)
- `primary_org` in the people table = most frequent meeting category, not actual employer
- Ubuntu shell is **bash** (changed from fish 2026-04-19)

## Related project folders / GitHub repos (separate from this pipeline)

Eoin's client-deliverable work lives in `~/Documents/{NTA,TBS,patents,whitepaper,aurum}/...`, with corresponding GitHub repos under `eoinlane/*` (mostly private). These are **separate from this pipeline repo** — the pipeline captures meetings/decisions ABOUT projects; the project folders contain the artefacts (tender drafts, course materials, papers, code, pitch decks).

Each leaf project folder has its own `CLAUDE.md`, and the umbrella folders (`~/Documents/{NTA,TBS,patents}/`) carry their own CLAUDE.md pointing to sub-projects, memory dossiers, and relevant `query_graph.py` invocations. Mapping of folder ↔ repo is documented in the memory dossier `project_github_integration_done_2026-05-08.md`. Notable: **`~/Documents/NTA/tenders/` is intentionally local-only** (drafts confidential; the sanitised hackathon-proposal track lives separately at `eoinlane/nta-tenders`).

Integration is deliberately light — bidirectional pointers via memory dossiers, no unified data model. Tier 2 (cross-cutting memory symlinked into every project folder) is live: `mac/setup-memory-symlinks.sh` is the script, the WatchPaths agent `com.eoin.refresh-memory-symlinks` re-runs it whenever pipeline memory or `~/Documents/` structure changes, and the nightly rebuild calls it as belt-and-braces. Auto-discovers any `~/Documents/<...>` folder containing a `CLAUDE.md` plus `~/paradigm`. The cross-cutting subset (user profile, behavioural feedback rules, client + person dossiers) lives in pipeline memory and is symlinked per-file into each project folder; pipeline-specific memory (voice catalog, calendar matcher quirks, GPU infra, graph audit) stays bound here. Tier 3 (deliverables-axis query layer) remains deferred and probably stays that way unless a concrete need emerges.
