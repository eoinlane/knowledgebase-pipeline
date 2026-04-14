"""
Upload the knowledge base markdown files to Open WebUI via its API.
Deletes old collections with the same name, creates a fresh one, uploads all files.
"""

import hashlib
import os
import sys
import json
import time
import requests
from pathlib import Path

STATE_FILE = Path.home() / ".local/bin/kb-upload-state.json"

BASE_URL = os.environ.get("OPEN_WEBUI_URL", "http://100.121.184.27:8080")
EMAIL = os.environ.get("OPEN_WEBUI_EMAIL", "eoinlane@gmail.com")
PASSWORD = os.environ.get("OPEN_WEBUI_PASSWORD", "el")
KB_DIR = Path.home() / "knowledge_base"
TIMEOUT = 30  # seconds per request

# ── Auth ──────────────────────────────────────────────────────────────────────
print("Authenticating...", flush=True)
r = requests.post(f"{BASE_URL}/api/v1/auths/signin",
                  json={"email": EMAIL, "password": PASSWORD},
                  timeout=TIMEOUT)
r.raise_for_status()
token = r.json()["token"]
headers = {"Authorization": f"Bearer {token}"}
print("  OK", flush=True)

# ── Delete old collections with same name, create fresh ───────────────────────
COLLECTION_NAME = "Eoin Lane — Meeting Notes & Knowledge Base"
COLLECTION_DESC = (
    "Full knowledge base of Eoin Lane's meeting recordings, transcripts, "
    "calendar events, and people — covering NTA, DCC, Diotima, Paradigm, "
    "ADAPT and other client work from 2025 onwards."
)

print("Cleaning up old collections...", flush=True)
r2 = requests.get(f"{BASE_URL}/api/v1/knowledge/", headers=headers, timeout=TIMEOUT)
all_collections = r2.json() if isinstance(r2.json(), list) else r2.json().get("items", [])
for col in all_collections:
    if col.get("name") == COLLECTION_NAME:
        cid = col["id"]
        rd = requests.delete(f"{BASE_URL}/api/v1/knowledge/{cid}/delete",
                             headers=headers, timeout=TIMEOUT)
        print(f"  Deleted: {cid[:8]} (status {rd.status_code})", flush=True)

print("Creating knowledge collection...", flush=True)
r = requests.post(f"{BASE_URL}/api/v1/knowledge/create",
                  headers=headers,
                  json={"name": COLLECTION_NAME, "description": COLLECTION_DESC},
                  timeout=TIMEOUT)
if r.status_code != 200:
    print(f"  Error: {r.status_code} {r.text}", flush=True)
    sys.exit(1)
collection_id = r.json()["id"]
print(f"  Created: {collection_id}", flush=True)

# ── Upload files ──────────────────────────────────────────────────────────────
def upload_file(filepath: Path) -> str | None:
    """Upload a file and return its file_id."""
    with open(filepath, "rb") as f:
        content = f.read()
    try:
        r = requests.post(
            f"{BASE_URL}/api/v1/files/",
            headers=headers,
            files={"file": (filepath.name, content, "text/markdown")},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()["id"]
    except requests.exceptions.Timeout:
        pass
    return None

def add_file_to_collection(file_id: str, collection_id: str) -> bool:
    try:
        r = requests.post(
            f"{BASE_URL}/api/v1/knowledge/{collection_id}/file/add",
            headers=headers,
            json={"file_id": file_id},
            timeout=TIMEOUT,
        )
        return r.status_code == 200
    except requests.exceptions.Timeout:
        return False

# Collect all markdown files
all_files = []
for subdir in ["meetings", "people", "topics"]:
    subpath = KB_DIR / subdir
    if subpath.exists():
        all_files += sorted(subpath.glob("*.md"))
readme = KB_DIR / "README.md"
if readme.exists():
    all_files.insert(0, readme)

print(f"\nUploading {len(all_files)} files...", flush=True)
success = 0
failed = 0
errors = []
file_state = {}

for i, filepath in enumerate(all_files):
    file_id = upload_file(filepath)
    if file_id:
        ok = add_file_to_collection(file_id, collection_id)
        if ok:
            success += 1
            file_state[filepath.name] = {
                "file_id": file_id,
                "hash": hashlib.sha256(filepath.read_bytes()).hexdigest(),
            }
        else:
            failed += 1
            errors.append(f"Add failed: {filepath.name}")
    else:
        failed += 1
        errors.append(f"Upload failed: {filepath.name}")

    if (i + 1) % 50 == 0:
        print(f"  {i+1}/{len(all_files)} — {success} ok, {failed} failed", flush=True)
        STATE_FILE.write_text(json.dumps(
            {"collection_id": collection_id, "files": file_state}, indent=2
        ))

print(f"\nDone: {success} uploaded, {failed} failed", flush=True)
if errors:
    print("Errors:")
    for e in errors[:10]:
        print(f"  {e}")

# Save final state for incremental uploads
STATE_FILE.write_text(json.dumps(
    {"collection_id": collection_id, "files": file_state}, indent=2
))
print(f"State saved ({len(file_state)} files tracked)", flush=True)

print(f"\nKnowledge base available at: {BASE_URL}")
print(f"Collection: {COLLECTION_NAME}")
