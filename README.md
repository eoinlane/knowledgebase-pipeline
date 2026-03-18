# Knowledge Base Pipeline

Automated pipeline that turns iPhone/Mac voice recordings into a searchable knowledge base in [Open WebUI](https://github.com/open-webui/open-webui).

## What it does

1. **Records** — voice notes captured on iPhone via Apple Notes
2. **Transcribes** — WhisperX (large-v2, CUDA) on Ubuntu GPU box, with speaker diarisation
3. **Classifies** — deepseek-r1:32b via Ollama assigns category, topic, summary, key people
4. **Builds** — markdown files per meeting, per person, and per topic — matched against Apple Calendar events
5. **Uploads** — to Open WebUI knowledge collection, queryable with Claude or deepseek

## Architecture

```
iPhone (Apple Notes voice memo)
    ↓ rsync every 5 min (launchd, OTHER Mac)
Ubuntu ~/audio-inbox/Notes/       ← .m4a files
    ↓ inotifywait (notes-watcher systemd service)
WhisperX large-v2 + pyannote diarisation
    ↓ .txt transcript
rsync → Mac iCloud ~/My Notes/
    ↓ deepseek-r1:32b via Ollama
classification.csv  (category, topic, summary, key_people)
    ↓ rsync → Mac iCloud ~/My Notes Analysis/
    ↓ launchd WatchPaths trigger (CSV changed)
build_knowledge_base.py  ← also reads Apple Calendar exports
    ↓
~/knowledge_base/  (meetings/, people/, topics/)
    ↓ rsync → Ubuntu ~/knowledge_base/
upload_knowledge_base_incremental.py
    ↓
Open WebUI collection (http://100.121.184.27:8080)
```

## Files

| File | Description |
|---|---|
| `build_knowledge_base.py` | Builds markdown KB from CSV + calendar data. Content-aware writes (only updates files when content changes). |
| `upload_knowledge_base_incremental.py` | Incremental upload to Open WebUI — hash-based, self-healing, no mtime drift. |
| `upload_knowledge_base.py` | Full rebuild upload — deletes and recreates the collection. Used by the 4am cron. |
| `rebuild-knowledge-base.sh` | 4am launchd job: export calendars → build → rsync → full upload. |
| `sync-knowledge-base.sh` | Incremental sync triggered by CSV changes via launchd WatchPaths. |
| `test-pipeline.sh` | 6am health check — verifies all services and sends a macOS notification. |
| `export-calendars.applescript` | Exports 7 Apple Calendar calendars to `/tmp/cal_*.txt` for KB matching. |

## Infrastructure

| Component | Location | Details |
|---|---|---|
| Ubuntu GPU box | `eoin@nvidiaubuntubox` (Tailscale: 100.121.184.27) | PNY RTX 5060 Ti 16GB, Ubuntu 24.04 |
| Open WebUI | `http://100.121.184.27:8080` | Running in Docker |
| LiteLLM proxy | Ubuntu port 4000 | Routes Claude API calls |
| Ollama | Ubuntu | deepseek-r1:32b (classification), deepseek-r1:14b |
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
