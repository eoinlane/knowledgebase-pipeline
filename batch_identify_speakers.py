#!/usr/bin/env python3
"""
Batch speaker identification for all transcripts.
Runs identify_speakers.py on every unprocessed transcript.

Usage:
  python3 batch_identify_speakers.py [options]

Options:
  --force      Re-run even if mapping already exists (skips confirmed)
  --dry-run    Show what would be processed without running
  --limit N    Process at most N transcripts (for testing)
  --category X Only process transcripts matching this category (e.g. DCC)

Designed to run overnight. Logs to ~/audio-inbox/speaker_id_batch.log
"""

import os, sys, json, re, csv, subprocess, time
from datetime import datetime

TRANS_DIR      = os.path.expanduser("~/audio-inbox/Transcriptions")
CSV_PATH       = os.path.expanduser("~/audio-inbox/classification.csv")
MAPPINGS_FILE  = os.path.expanduser("~/speaker_mappings.json")
KB_DIR         = os.path.expanduser("~/knowledge_base/meetings")
LOG_FILE       = os.path.expanduser("~/audio-inbox/speaker_id_batch.log")
IDENTIFY_SCRIPT = os.path.expanduser("~/identify_speakers.py")
VENV_PYTHON    = os.path.expanduser("~/whisper-env/bin/python3")

# --- Parse args ---
force    = "--force"   in sys.argv
dry_run  = "--dry-run" in sys.argv
limit    = None
category_filter = None
for i, arg in enumerate(sys.argv):
    if arg == "--limit" and i + 1 < len(sys.argv):
        limit = int(sys.argv[i + 1])
    if arg == "--category" and i + 1 < len(sys.argv):
        category_filter = sys.argv[i + 1].lower()

# --- Load existing mappings ---
mappings = {}
if os.path.exists(MAPPINGS_FILE):
    with open(MAPPINGS_FILE) as f:
        mappings = json.load(f)

# --- Load CSV for category lookup ---
csv_index = {}  # uuid → {category, key_people}
if os.path.exists(CSV_PATH):
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            fname = row.get("filename", "").replace(".txt", "")
            csv_index[fname] = {
                "category": row.get("category", ""),
                "key_people": row.get("key_people", ""),
            }

# --- Build list of transcripts to process ---
all_txts = sorted(f for f in os.listdir(TRANS_DIR) if f.endswith(".txt"))

to_process = []
skipped_confirmed = 0
skipped_mapped    = 0
skipped_no_speakers = 0

for fname in all_txts:
    uuid = fname.replace(".txt", "")
    path = os.path.join(TRANS_DIR, fname)

    # Check for SPEAKER_XX labels
    try:
        with open(path, errors="replace") as f:
            snippet = f.read(500)
        # Quick check in first 500 chars; if not there, read more
        if "SPEAKER_" not in snippet:
            with open(path, errors="replace") as f:
                full = f.read()
            if not re.search(r'\[SPEAKER_\d+\]', full):
                skipped_no_speakers += 1
                continue
    except Exception:
        continue

    # Skip confirmed mappings always
    if uuid in mappings and mappings[uuid].get("confirmed"):
        skipped_confirmed += 1
        continue

    # Skip already-mapped if not forcing
    if not force and uuid in mappings:
        skipped_mapped += 1
        continue

    # Category filter
    if category_filter:
        cat = csv_index.get(uuid, {}).get("category", "").lower()
        if category_filter not in cat:
            continue

    to_process.append((uuid, path))

if limit:
    to_process = to_process[:limit]

# --- Summary ---
print(f"\nBatch Speaker Identification")
print(f"{'=' * 50}")
print(f"Total transcripts:        {len(all_txts)}")
print(f"No speaker labels:        {skipped_no_speakers}")
print(f"Already confirmed:        {skipped_confirmed}")
print(f"Already mapped (skip):    {skipped_mapped}")
print(f"To process:               {len(to_process)}")
if category_filter:
    print(f"Category filter:          {category_filter}")
if limit:
    print(f"Limit:                    {limit}")
print(f"Dry run:                  {dry_run}")
print(f"Log:                      {LOG_FILE}")
print(f"{'=' * 50}\n")

if not to_process:
    print("Nothing to do.")
    sys.exit(0)

if dry_run:
    print("Would process:")
    for uuid, path in to_process:
        cat = csv_index.get(uuid, {}).get("category", "?")
        print(f"  {uuid[:20]}...  [{cat}]")
    sys.exit(0)

# --- Run ---
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}: {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

log(f"Batch started — {len(to_process)} transcripts to process")

ok = 0
failed = 0
start_all = time.time()

for i, (uuid, path) in enumerate(to_process, 1):
    cat = csv_index.get(uuid, {}).get("category", "?")
    elapsed = time.time() - start_all
    avg = elapsed / i if i > 1 else 0
    remaining = avg * (len(to_process) - i + 1)
    eta = f"{int(remaining // 60)}m" if remaining else "?"

    log(f"[{i}/{len(to_process)}] {uuid[:36]} [{cat}] ETA: {eta}")

    try:
        result = subprocess.run(
            [VENV_PYTHON, IDENTIFY_SCRIPT, path, CSV_PATH],
            capture_output=True, text=True, timeout=360
        )
        if result.returncode == 0:
            # Log the key output line (Notes/Speaker lines)
            for line in result.stdout.splitlines():
                if any(x in line for x in ["Notes:", "→", "Attendees", "FAILED", "nothing"]):
                    log(f"  {line.strip()}")
            ok += 1
        else:
            log(f"  FAILED (exit {result.returncode}): {result.stderr[:200]}")
            failed += 1
    except subprocess.TimeoutExpired:
        log(f"  TIMEOUT after 360s")
        failed += 1
    except Exception as e:
        log(f"  ERROR: {e}")
        failed += 1

elapsed_total = int(time.time() - start_all)
log(f"Batch complete — {ok} OK, {failed} failed, {elapsed_total}s total")
print(f"\nDone. {ok} identified, {failed} failed.")
print(f"Run python3 review_speakers.py to confirm mappings.")
