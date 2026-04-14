"""
Incremental upload to Open WebUI — hash-based change detection.

Derives ground truth from the API on every run. No mtime tracking.

State file: ~/.local/bin/kb-upload-state.json
  {"collection_id": "...", "files": {"filename.md": {"file_id": "...", "hash": "..."}}}

Change detection:
  - SHA-256 of local file compared against stored hash
  - If stored hash matches local hash and file_id still exists remotely → skip
  - If content already exists in system under same/different name (orphan rescue) → link it
  - Otherwise → upload fresh

Self-healing:
  - Lost/corrupt state: all files detected as changed; orphan rescue re-links existing copies
    without re-uploading (add_to_collection is idempotent — returns 200 if already linked)
  - After nightly full rebuild: state has old mtime format (no hash); first incremental run
    re-links all files via orphan rescue, rebuilds state with hashes; subsequent runs are fast

Deletions are handled by the nightly full rebuild (upload_knowledge_base.py).
"""

import hashlib
import json
import os
import sys
import time
import requests
from pathlib import Path

BASE_URL = os.environ.get("OPEN_WEBUI_URL", "http://100.121.184.27:8080")
EMAIL = os.environ.get("OPEN_WEBUI_EMAIL", "eoinlane@gmail.com")
PASSWORD = os.environ.get("OPEN_WEBUI_PASSWORD", "el")
KB_DIR = Path.home() / "knowledge_base"
STATE_FILE = Path.home() / ".local/bin/kb-upload-state.json"
TIMEOUT = 30

COLLECTION_NAME = "Eoin Lane — Meeting Notes & Knowledge Base"
COLLECTION_DESC = (
    "Full knowledge base of Eoin Lane's meeting recordings, transcripts, "
    "calendar events, and people — covering NTA, DCC, Diotima, Paradigm, "
    "ADAPT and other client work from 2025 onwards."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Auth ──────────────────────────────────────────────────────────────────────
def authenticate():
    """Authenticate and return headers. Called on startup and on 401 errors."""
    r = requests.post(f"{BASE_URL}/api/v1/auths/signin",
                      json={"email": EMAIL, "password": PASSWORD}, timeout=TIMEOUT)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}

print("Authenticating...", flush=True)
headers = authenticate()
last_auth = time.time()
print("  OK", flush=True)

# ── Validate / discover collection ────────────────────────────────────────────
state = load_state()
collection_id = state.get("collection_id")

if collection_id:
    r = requests.get(f"{BASE_URL}/api/v1/knowledge/{collection_id}",
                     headers=headers, timeout=TIMEOUT)
    if r.status_code != 200:
        print(f"  Stored collection {collection_id[:8]} gone — searching...", flush=True)
        collection_id = None

if not collection_id:
    r = requests.get(f"{BASE_URL}/api/v1/knowledge/", headers=headers, timeout=TIMEOUT)
    cols = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
    match = next((c for c in cols if c.get("name") == COLLECTION_NAME), None)
    if match:
        collection_id = match["id"]
        print(f"  Found collection: {collection_id[:8]}", flush=True)
    else:
        r = requests.post(f"{BASE_URL}/api/v1/knowledge/create", headers=headers,
                          json={"name": COLLECTION_NAME, "description": COLLECTION_DESC},
                          timeout=TIMEOUT)
        r.raise_for_status()
        collection_id = r.json()["id"]
        print(f"  Created collection: {collection_id[:8]}", flush=True)
        state = {}  # Fresh collection — drop stale state

state["collection_id"] = collection_id
file_state = state.get("files", {})  # {filename: {file_id, hash}}

# ── Fetch all remote files ────────────────────────────────────────────────────
print("Fetching remote file index...", flush=True)
r = requests.get(f"{BASE_URL}/api/v1/files/", headers=headers, timeout=60)
r.raise_for_status()
all_remote = r.json()

remote_by_id = {f["id"]: f for f in all_remote}

# For duplicates, keep the newest entry per filename and per hash
remote_by_name: dict = {}
remote_by_hash: dict = {}
for f in all_remote:
    for lookup, key in [(remote_by_name, f["filename"]), (remote_by_hash, f["hash"])]:
        if key not in lookup or f["created_at"] > lookup[key]["created_at"]:
            lookup[key] = f

print(f"  {len(all_remote)} files ({len(remote_by_name)} unique names)", flush=True)

# ── Collect local files ───────────────────────────────────────────────────────
local_files: dict[str, Path] = {}
for subdir in ["meetings", "people", "topics"]:
    subpath = KB_DIR / subdir
    if subpath.exists():
        for f in subpath.glob("*.md"):
            local_files[f.name] = f
readme = KB_DIR / "README.md"
if readme.exists():
    local_files["README.md"] = readme

print(f"Local KB: {len(local_files)} files", flush=True)

# ── Compute diff ──────────────────────────────────────────────────────────────
to_process = []  # (filepath, local_hash, old_file_id or None)

for name, filepath in local_files.items():
    local_hash = sha256(filepath)
    entry = file_state.get(name, {})
    file_id = entry.get("file_id")

    # Up to date: same hash, file still exists remotely
    if file_id and entry.get("hash") == local_hash and file_id in remote_by_id:
        continue

    # Permanently unaddable for this content (empty/duplicate) — skip until content changes
    if entry.get("skip") and entry.get("hash") == local_hash:
        continue

    to_process.append((filepath, local_hash, file_id))

print(f"Changes: {len(to_process)} to upload", flush=True)

if not to_process:
    print("Nothing to do.", flush=True)
    sys.exit(0)

# ── Helpers ───────────────────────────────────────────────────────────────────
def refresh_auth_if_needed():
    """Re-authenticate if token is older than 10 minutes."""
    global headers, last_auth
    if time.time() - last_auth > 600:
        headers = authenticate()
        last_auth = time.time()


def refresh_auth_on_401():
    """Force re-authentication after a 401 error."""
    global headers, last_auth
    print("    Re-authenticating...", flush=True)
    headers = authenticate()
    last_auth = time.time()


def upload_file(filepath: Path) -> str | None:
    refresh_auth_if_needed()
    for attempt in range(2):
        try:
            r = requests.post(f"{BASE_URL}/api/v1/files/", headers=headers,
                              files={"file": (filepath.name, filepath.read_bytes(), "text/markdown")},
                              timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()["id"]
            if r.status_code == 401 and attempt == 0:
                refresh_auth_on_401()
                continue
            print(f"    Upload {r.status_code}: {r.text[:120]}", flush=True)
        except requests.exceptions.Timeout:
            print(f"    Timeout: {filepath.name}", flush=True)
        break
    return None


def add_to_collection(file_id: str) -> str:
    """Returns 'ok', 'permanent' (empty/duplicate content), or 'error'."""
    for attempt in range(2):
        try:
            r = requests.post(f"{BASE_URL}/api/v1/knowledge/{collection_id}/file/add",
                              headers=headers, json={"file_id": file_id}, timeout=TIMEOUT)
            if r.status_code == 200:
                return "ok"
            if r.status_code == 401 and attempt == 0:
                refresh_auth_on_401()
                continue
            detail = r.text.lower()
            if "empty" in detail or "duplicate" in detail:
                return "permanent"
            print(f"    add_to_collection {r.status_code}: {r.text[:120]}", flush=True)
            return "error"
        except requests.exceptions.Timeout:
            return "error"
    return "error"


def remove_from_collection(file_id: str):
    try:
        requests.post(f"{BASE_URL}/api/v1/knowledge/{collection_id}/file/remove",
                      headers=headers, json={"file_id": file_id}, timeout=TIMEOUT)
    except Exception:
        pass


def delete_file(file_id: str):
    try:
        requests.delete(f"{BASE_URL}/api/v1/files/{file_id}",
                        headers=headers, timeout=TIMEOUT)
    except Exception:
        pass


# ── Apply changes ─────────────────────────────────────────────────────────────
uploaded = rescued = skipped = failed = 0

for i, (filepath, local_hash, old_file_id) in enumerate(to_process):
    name = filepath.name

    # Orphan rescue: content already exists in system — link it rather than re-upload
    existing = remote_by_hash.get(local_hash)
    if existing:
        if existing["id"] != old_file_id:
            # Remove the stale version before re-linking
            if old_file_id and old_file_id in remote_by_id:
                remove_from_collection(old_file_id)
                delete_file(old_file_id)
        result = add_to_collection(existing["id"])  # idempotent: ok even if already linked
        if result == "ok":
            file_state[name] = {"file_id": existing["id"], "hash": local_hash}
            rescued += 1
        elif result == "permanent":
            file_state[name] = {"file_id": None, "hash": local_hash, "skip": True}
            skipped += 1
            print(f"    Skipping {name} (unaddable — empty/duplicate content)", flush=True)
        else:
            failed += 1
    else:
        # Fresh upload needed
        if old_file_id and old_file_id in remote_by_id:
            remove_from_collection(old_file_id)
            delete_file(old_file_id)

        file_id = upload_file(filepath)
        if file_id:
            result = add_to_collection(file_id)
            if result == "ok":
                file_state[name] = {"file_id": file_id, "hash": local_hash}
                uploaded += 1
            elif result == "permanent":
                file_state[name] = {"file_id": None, "hash": local_hash, "skip": True}
                delete_file(file_id)  # clean up the upload we couldn't use
                skipped += 1
                print(f"    Skipping {name} (unaddable — empty/duplicate content)", flush=True)
            else:
                # Transient error — try orphan rescue as fallback
                match = remote_by_hash.get(local_hash)
                if match and add_to_collection(match["id"]) == "ok":
                    file_state[name] = {"file_id": match["id"], "hash": local_hash}
                    delete_file(file_id)  # clean up unused upload
                    rescued += 1
                else:
                    failed += 1
        else:
            failed += 1

    if (i + 1) % 25 == 0:
        print(f"  {i+1}/{len(to_process)} — {uploaded} uploaded, {rescued} rescued, "
              f"{skipped} skipped, {failed} failed", flush=True)
        save_state({"collection_id": collection_id, "files": file_state})

    time.sleep(0.05)

state["files"] = file_state
save_state(state)

parts = []
if uploaded:
    parts.append(f"{uploaded} uploaded")
if rescued:
    parts.append(f"{rescued} re-linked")
if skipped:
    parts.append(f"{skipped} skipped (unaddable)")
if failed:
    parts.append(f"{failed} failed")
print(f"\nDone: {', '.join(parts) or 'no changes'}", flush=True)

if failed:
    sys.exit(1)
