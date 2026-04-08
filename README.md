# Knowledge Base Pipeline

Automated pipeline that turns iPhone/Mac voice recordings into a searchable knowledge base in [Open WebUI](https://github.com/open-webui/open-webui).

## What it does

1. **Records** — voice notes captured on iPhone via Apple Notes
2. **Transcribes** — WhisperX (large-v2, CUDA) on Ubuntu GPU box, with speaker diarisation + ECAPA-TDNN voice embeddings per speaker
3. **Classifies** — qwen2.5:14b via Ollama assigns category, topic, summary, key people
4. **Identifies speakers** — maps SPEAKER_XX labels to real names using voice fingerprinting + LLM; rewrites transcript in-place
5. **Builds** — markdown files per meeting, per person, and per topic — matched against Apple Calendar events
6. **Uploads** — to Open WebUI knowledge collection, queryable with Claude or deepseek

## Architecture

```
iPhone (Apple Notes voice memo)
    ↓ rsync every 5 min (launchd)
Ubuntu ~/audio-inbox/Notes/       ← .m4a files
    ↓ inotifywait (notes-watcher systemd service)
1. transcribe_single.py  (WhisperX large-v3 + pyannote diarisation)
   → .txt transcript + ECAPA-TDNN voice embeddings
    ↓
2. classify_transcript.py  (qwen2.5:14b via ollama-box)
   → classification.csv  (category, topic, summary, key_people)
    ↓
3. identify_speakers.py  (voice catalog match → LLM fallback)
   → rewrites transcript SPEAKER_XX → [Real Name]
    ↓
4. reclassify_by_speaker.py  (override LLM category from voice-identified speakers)
    ↓
5. extract_meeting_insights.py  (qwen2.5:14b)
   → action items, decisions, follow-ups, open questions → JSON
    ↓ rsync to Mac
6. build_knowledge_base.py  ← calendar attendees (timestamp match) + insights
   → ~/knowledge_base/  (meetings/, people/, topics/)
    ↓
7. upload_knowledge_base_incremental.py → Open WebUI
```

## Repository Structure

```
ubuntu/          — scripts that run on Ubuntu GPU box (9 scripts)
mac/             — scripts that run on Mac (8 scripts)
mac/launchd/     — launchd agent scripts (7 scripts)
shared/          — shared config: OLLAMA_URL, MODEL, PERSON_CATEGORY, name expansions
tools/           — benchmark_models.py
tests/           — 62 tests (pytest)
Makefile         — deploy-ubuntu, deploy-mac, test, clean-ubuntu
```

Scripts run via symlinks: `~/identify_speakers.py → repo/ubuntu/identify_speakers.py`. Deploy with `make deploy-ubuntu` / `make deploy-mac`.

## Key Files

| File | Description |
|---|---|
| `ubuntu/transcribe_single.py` | WhisperX transcription + diarisation + ECAPA-TDNN voice embeddings |
| `ubuntu/classify_transcript.py` | LLM classification: category, topic, summary, key_people |
| `ubuntu/identify_speakers.py` | Voice catalog match first, LLM fallback. Rewrites transcript in-place |
| `ubuntu/reclassify_by_speaker.py` | Override LLM category if voice-identified speakers map to one org |
| `ubuntu/extract_meeting_insights.py` | Extract action items, decisions, follow-ups, open questions |
| `ubuntu/watchdog-transcribe.sh` | Systemd timer (30 min): runs full pipeline, retries failures |
| `mac/build_knowledge_base.py` | Builds markdown KB from CSV + calendar (timestamp-matched attendees) + insights |
| `mac/launchd/pipeline-health-check.sh` | Hourly: checks Ubuntu, FreeBSD, ollama-box, services, backlog |
| `shared/config.py` | OLLAMA_URL, MODEL, PERSON_CATEGORY, KEEP_CATEGORIES |
| `shared/name_expansions.py` | WhisperX mishearing → full name tables per category |
| `sync-knowledge-base.sh` | Incremental sync triggered by CSV changes via launchd WatchPaths. |
| `test-pipeline.sh` | 6am health check — verifies all services and sends a macOS notification. |
| `export-calendars.applescript` | Exports 7 Apple Calendar calendars to `/tmp/cal_*.txt` for KB matching. |

## Infrastructure

| Component | Location | Details |
|---|---|---|
| Ubuntu GPU box | `eoin@nvidiaubuntubox` (Tailscale: 100.121.184.27) | PNY RTX 5060 Ti 16GB, Ubuntu 24.04 |
| Open WebUI | `http://100.121.184.27:8080` | Running in Docker |
| LiteLLM proxy | Ubuntu port 4000 | Routes Claude API calls |
| Ollama | ollama-box (192.168.0.70) | qwen2.5:14b (classification + speaker ID) |
| WhisperX | Ubuntu `~/whisper-env/` | large-v2, CUDA, float16, English |

## Mac launchd Agents

| Label | Script | Schedule |
|---|---|---|
| `com.eoin.rebuild-knowledge-base` | `rebuild-knowledge-base.sh` | Daily 4am |
| `com.eoin.sync-knowledge-base` | `sync-knowledge-base.sh` | WatchPaths: CSV change |
| `com.eoin.test-pipeline` | `test-pipeline.sh` | Daily 6am |

## Knowledge Base

- **Output**: `~/knowledge_base/` on Mac and Ubuntu
  - `meetings/` — one file per recording (date, category, summary, action items, calendar match, attendees, full transcript)
  - `people/` — one file per person (all meetings they appear in)
  - `topics/` — category index files (NTA, DCC, Diotima, Paradigm, ADAPT, TBS, etc.)
- **Open WebUI collection**: "Eoin Lane — Meeting Notes & Knowledge Base"
- **RAG settings**: CHUNK_SIZE 4000, CHUNK_OVERLAP 400, TOP_K 20
- **Recommended query model**: `claude-sonnet-4-6` via LiteLLM proxy

## Upload Design

The incremental upload (`upload_knowledge_base_incremental.py`) uses hash-based change detection:

- Fetches all remote files from Open WebUI API on each run — derives state from the API, not a local file
- Computes SHA-256 of each local file and compares against stored hash
- **Orphan rescue**: if a file's content already exists in the system (by hash), it links that file instead of re-uploading — automatically recovers from interrupted uploads
- **Permanent skip**: files that Open WebUI can't add (empty content / duplicate vector chunks) are marked `skip: true` in state with their hash, so they don't retry unless content changes
- State file: `~/.local/bin/kb-upload-state.json` — `{filename: {file_id, hash}}` or `{filename: {file_id: null, hash, skip: true}}`

The nightly full rebuild (`upload_knowledge_base.py`) deletes and recreates the collection from scratch — clearing any accumulated vector store issues and retrying previously skipped files.

## iCloud Locking

Files in `~/Library/Mobile Documents/com~apple~CloudDocs/` can be locked by iCloud's sync daemon (EDEADLK, errno 11). The build script uses `icloud_read()` which copies the file to `/tmp` before reading — completely sidestepping the lock.

```python
def icloud_read(path, retries=8, delay=10, **kwargs):
    tmp_path = f"/tmp/_icloud_{os.getpid()}_{os.path.basename(path)}"
    for attempt in range(retries):
        try:
            shutil.copy2(str(path), tmp_path)
            with open(tmp_path, **kwargs) as f:
                return f.read()
            ...
```

## Speaker Identification

Transcripts are automatically rewritten so `[SPEAKER_00]` becomes `[Eoin Lane]` etc. The system uses two complementary approaches:

**Voice fingerprinting** — ECAPA-TDNN (192-dim) embeddings extracted during transcription and stored per speaker in `~/audio-inbox/Embeddings/{UUID}.json`. On each new recording, embeddings are compared (cosine similarity) against `~/voice_catalog.json`. Score ≥ 0.80 = high confidence auto-assign; ≥ 0.70 = medium. No LLM call needed for known speakers.

**LLM identification** — For unmatched speakers, qwen2.5:14b is called with:
- Full attendee list from calendar (or expanded CSV key_people)
- Hard constraints extracted from transcript (e.g. "SPEAKER_02 addressed Kizzer, Richie, Stephen — so is NOT any of them")
- Speech pattern examples from `~/speaker_registry.json` (grows with each confirmation)

The system learns with each confirmation via `review_speakers.py`:
- Speech samples added to registry (LLM gets better few-shot examples)
- Voice embeddings added to catalog (voice matching gets more reliable)

**Category-aware name expansion** in `identify_speakers.py` maps short/mishearing names to full names (e.g. DCC: "kizzer" → "Khizer Ahmed Biyabani", "chris" → "Christopher Kelly").

**Review and confirm mappings:**
```bash
ssh eoin@nvidiaubuntubox "python3 ~/review_speakers.py"
# Commands: [y] confirm  [e] edit names  [s] skip  [q] quit
```

**Run batch identification on existing transcripts (overnight):**
```bash
ssh eoin@nvidiaubuntubox
nohup bash -c 'source ~/whisper-env/bin/activate && python3 ~/batch_identify_speakers.py' \
  > ~/audio-inbox/speaker_id_batch.log 2>&1 &
# Monitor:
tail -f ~/audio-inbox/speaker_id_batch.log
```

**Data files (Ubuntu):**

| File | Contents |
|---|---|
| `~/audio-inbox/Embeddings/{UUID}.json` | Per-recording per-speaker ECAPA-TDNN embeddings |
| `~/voice_catalog.json` | Per-person voice embeddings (rolling 20), grows with confirmations |
| `~/speaker_registry.json` | Per-person speech samples for LLM few-shot, grows with confirmations |
| `~/speaker_mappings.json` | Per-recording mappings + confirmed flag |

## Manual Operations

**Re-run build and incremental upload:**
```bash
python3 ~/build_knowledge_base.py
python3 ~/upload_knowledge_base_incremental.py
```

**Re-classify a recording that timed out:**
```bash
# On Ubuntu
python3 ~/classify_transcript.py ~/audio-inbox/Transcriptions/<uuid>.txt ~/audio-inbox/classification.csv
```

**Re-run speaker identification on a single recording:**
```bash
# On Ubuntu
source ~/whisper-env/bin/activate
python3 ~/identify_speakers.py ~/audio-inbox/Transcriptions/<uuid>.txt ~/audio-inbox/classification.csv
```

**Full rebuild:**
```bash
bash ~/.local/bin/rebuild-knowledge-base.sh
```

**Check pipeline health:**
```bash
tail -50 ~/.local/bin/rebuild-knowledge-base.log
tail -50 ~/.local/bin/sync-knowledge-base.log
ssh eoin@nvidiaubuntubox "sudo systemctl status notes-watcher"
```
