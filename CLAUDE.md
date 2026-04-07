# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

An automated pipeline that turns iPhone voice memos into a searchable knowledge base. Recordings flow from iPhone → Ubuntu GPU box (transcription + speaker ID) → Mac (KB build) → Open WebUI (RAG). Scripts in this repo run on **both Mac and Ubuntu** — the split matters.

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
# Or just the KB + contacts:
python3 ~/build_knowledge_base.py
python3 ~/knowledgebase-pipeline/build_contacts_db.py
```

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

### Mac vs Ubuntu Split

Scripts that run on **Ubuntu** (GPU required): `transcribe_single.py`, `identify_speakers.py`, `review_speakers.py`, `batch_identify_speakers.py`, `watch-and-transcribe.sh`, `watchdog-transcribe.sh`. These are deployed by copying to `~/` on Ubuntu — they do not run from the repo path.

Scripts that run on **Mac**: `build_knowledge_base.py`, `build_contacts_db.py`, `contacts_viewer.py`, `apply_kb_corrections.py`, `process_inbox.py`, `upload_knowledge_base_incremental.py`.

### Data Flow

```
classification.csv (iCloud)  +  Apple Calendar exports (/tmp/cal_*.txt)
    ↓
build_knowledge_base.py  →  ~/knowledge_base/meetings/*.md
                                             people/*.md
                                             topics/*.md
    ↓
apply_kb_corrections.py  ←  ~/kb_corrections.json (manual overrides)
    ↓
build_contacts_db.py  →  ~/contacts.db  (meetings, people, attendees tables)
                           entity_resolution.py  →  merge_suggestions table
    ↓
upload_knowledge_base_incremental.py  →  Open WebUI
```

### Corrections Layer

`~/kb_corrections.json` is the **durable override layer** — it survives nightly rebuilds. Structure:
```json
{
  "people": { "RawName": { "name": "Full Name", "title": "...", "org": "..." } },
  "meetings": { "filename.md": { "topic": "...", "tags": [...], "people_corrections": {} } }
}
```
`apply_kb_corrections.py` patches KB markdown files after each build. `contacts_viewer.py` writes to this file when you edit names/orgs/topics in the UI. Changes propagate on the next rebuild.

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

### Inbox Processing

`process_inbox.py` watches `~/inbox/` (via launchd WatchPaths). Supported: `.pdf`, `.docx`, `.pptx`, `.eml`, `.txt`, `.md`, images. Classification via LiteLLM proxy at `http://100.121.184.27:4000` using `claude-haiku-4-5`. Outputs to `~/knowledge_base/documents/`. Emails (`.eml`) extract body + embedded attachments into a single KB doc with `type: email` frontmatter.

### Upload Design

`upload_knowledge_base_incremental.py` is hash-based — derives state from the Open WebUI API on each run (no local state file dependency). If a file's content already exists by SHA-256 hash, it links rather than re-uploads (orphan rescue). Files that Open WebUI permanently rejects are marked `skip: true` and skipped on future runs unless content changes.

### iCloud File Access

All reads from `~/Library/Mobile Documents/com~apple~CloudDocs/` go through `icloud_read()` in `build_knowledge_base.py`, which copies to `/tmp` before reading. This sidesteps EDEADLK locking from iCloud's sync daemon. Never attempt direct reads or retries on iCloud paths.

### Speaker Identification

`identify_speakers.py` (Ubuntu) rewrites `[SPEAKER_XX]` labels in transcripts using:
1. Voice matching: cosine similarity of ECAPA-TDNN embeddings against `~/voice_catalog.json` (≥0.80 = high confidence, ≥0.70 = medium)
2. LLM fallback: deepseek-r1:32b with calendar attendees, name-call cues from transcript, and speech samples from `~/speaker_registry.json`

The name expansion table inside `identify_speakers.py` maps category-specific mishearings to full names (e.g. DCC: `"kizzer"` → `"Khizer Ahmed Biyabani"`). The script body is wrapped in `if __name__ == "__main__":` so functions are importable for testing.

`transcribe_single.py` uses WhisperX `large-v3` with post-processing `dedupe_segments()` to strip hallucinated repeated segments. Also extracts ECAPA-TDNN voice embeddings per speaker. LLM inference (classification + speaker ID) runs on ollama-box (192.168.0.70), completely separate from the Ubuntu transcription GPU.

## Infrastructure

| Component | Details |
|---|---|
| Ubuntu | `eoin@nvidiaubuntubox`, Tailscale `100.121.184.27`, SSH key auth, password `el` |
| Open WebUI | `http://100.121.184.27:8080` |
| LiteLLM proxy | Ubuntu port 4000, models: `claude-sonnet-4-6`, `claude-haiku-4-5` |
| ollama-box | `192.168.0.70:11434`, Debian 13 bhyve VM on FreeBSD (192.168.0.14), RTX 4060 8GB, `deepseek-r1:14b` (~8 tok/s). Start VM: `ssh eoin@192.168.0.14 "echo el \| sudo -S vm start ollama-box"` |
| WhisperX | Ubuntu RTX 5060 Ti 16GB, model `large-v3`, CUDA float16. `watch-and-transcribe.sh` handles new files via inotify; `watchdog-transcribe.sh` runs every 30 min via systemd timer to catch misses and retry failed classifications |
| WhisperX env | Ubuntu `~/whisper-env/` — always activate before running transcription scripts |

## KB File Conventions

- Meeting filenames: `YYYY-MM-DD_HHMM_CATEGORY_slug.md`
- People filenames: slugified full name (e.g. `cathal-murphy.md`) — Eoin Lane has no people file
- Categories: `NTA`, `DCC`, `DFB`, `ADAPT`, `Diotima`, `Paradigm`, `TBS`, `other:*`
- "Owen Lane" in transcripts = Eoin Lane (WhisperX mishearing)
- `primary_org` in the people table = most frequent meeting category, not actual employer
- Ubuntu shell is **fish** — wrap background commands: `bash -c '...'`
