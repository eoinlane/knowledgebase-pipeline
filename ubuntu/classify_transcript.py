"""
Classifies a transcript using qwen2.5:14b via ollama-box, with LiteLLM/Haiku fallback.
Usage: python3 classify_transcript.py <transcript_txt> <csv_path>
"""
import sys, os, json, csv, re
from datetime import datetime
import urllib.request

PIPELINE_DIR = os.environ.get("PIPELINE_DIR", os.path.expanduser("~/knowledgebase-pipeline"))
if os.path.isdir(PIPELINE_DIR) and PIPELINE_DIR not in sys.path:
    sys.path.insert(0, PIPELINE_DIR)
try:
    from shared.config import OLLAMA_URL, MODEL, HAIKU_MODEL, LITELLM_URL
except ImportError:
    OLLAMA_URL = "http://192.168.0.70:11434/api/chat"
    MODEL = "qwen2.5:14b"
    HAIKU_MODEL = "claude-haiku-4-5"
    LITELLM_URL = "http://localhost:4000/v1/chat/completions"

# The classify prompt is the source of truth — if shared/prompts.py isn't
# importable, fail loudly. Running classification with a None/empty prompt
# would silently produce garbage. No hardcoded fallback by design.
from shared.prompts import CLASSIFY_SYSTEM_PROMPT as SYSTEM_PROMPT

LITELLM_MODEL = HAIKU_MODEL

CSV_PATH = sys.argv[2]
transcript_path = sys.argv[1]

with open(transcript_path) as f:
    content = f.read()

# Extract filename and date from transcript header
filename = ""
recorded_at = ""
for line in content.splitlines()[:3]:
    if line.startswith("File:"):
        filename = line.replace("File:", "").strip().replace(".m4a", "")
    if line.startswith("Recorded:"):
        recorded_at = line.replace("Recorded:", "").strip()

# SYSTEM_PROMPT is imported above from shared.prompts.CLASSIFY_SYSTEM_PROMPT.
# Single source of truth — see shared/prompts.py.

USER_PROMPT = f"Classify this transcript:\n\n{content[:6000]}"

messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": USER_PROMPT}
]


def call_ollama(messages):
    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0}
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    return result["message"]["content"].strip()


def call_litellm(messages):
    payload = json.dumps({
        "model": LITELLM_MODEL,
        "messages": messages,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        LITELLM_URL, data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"].strip()


# Try ollama first, fall back to Haiku
try:
    raw = call_ollama(messages)
    model_used = MODEL
except Exception as e:
    print(f"  Ollama unavailable ({e}), falling back to Haiku")
    try:
        raw = call_litellm(messages)
        model_used = LITELLM_MODEL
    except Exception as e2:
        print(f"  LiteLLM also failed: {e2}", file=sys.stderr)
        sys.exit(1)

# Strip <think>...</think> blocks that deepseek-r1 emits
raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

# Extract JSON from response
json_match = re.search(r"\{.*\}", raw, re.DOTALL)
if not json_match:
    print(f"Could not parse JSON from response:\n{raw}", file=sys.stderr)
    sys.exit(1)

try:
    classification = json.loads(json_match.group())
except json.JSONDecodeError as e:
    print(f"JSON decode error: {e}\nRaw: {raw}", file=sys.stderr)
    sys.exit(1)

# Validate category against the enumerated set. Previously this defaulted to
# 'other:blank' on any missing/invalid value, which silently dropped meetings
# from the KB (build_knowledge_base.py filters other:blank). Now we exit non-zero
# so the watchdog retries, and an off-list value gets one chance to come back
# corrected before we accept it.
VALID_CATEGORIES = {
    "NTA", "DCC", "Diotima", "ADAPT", "TBS", "Paradigm",
    "other:blank", "other:personal", "other:conference", "other:lgma",
}

category = classification.get("category")
if not category:
    print(f"Classification missing 'category' field — refusing to default. Raw: {raw}",
          file=sys.stderr)
    sys.exit(2)
if category not in VALID_CATEGORIES:
    # Retry once with a stricter prompt that names the valid set explicitly.
    print(f"  LLM returned off-list category {category!r}; retrying with stricter prompt",
          file=sys.stderr)
    strict_messages = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content":
            f"That category is not in the allowed set. "
            f"Pick exactly one of: {sorted(VALID_CATEGORIES)}. "
            f"Respond with only the JSON object."},
    ]
    try:
        raw2 = call_ollama(strict_messages) if model_used == MODEL else call_litellm(strict_messages)
    except Exception as e:
        print(f"  Retry failed: {e}", file=sys.stderr)
        sys.exit(3)
    raw2 = re.sub(r"<think>.*?</think>", "", raw2, flags=re.DOTALL).strip()
    json_match2 = re.search(r"\{.*\}", raw2, re.DOTALL)
    if not json_match2:
        print(f"  Retry produced no JSON. Raw: {raw2}", file=sys.stderr)
        sys.exit(3)
    try:
        classification = json.loads(json_match2.group())
    except json.JSONDecodeError as e:
        print(f"  Retry JSON decode error: {e}\nRaw: {raw2}", file=sys.stderr)
        sys.exit(3)
    category = classification.get("category")
    if category not in VALID_CATEGORIES:
        print(f"  Retry also returned invalid category {category!r}. Giving up.",
              file=sys.stderr)
        sys.exit(3)

topic      = classification.get("topic", "")
summary    = classification.get("summary", "")
key_people = classification.get("key_people", "")

print(f"  Category: {category}")
print(f"  Topic:    {topic}")

# Update CSV — add row if filename not present, update if it is
rows = []
fieldnames = ["filename", "date", "category", "summary", "key_people", "topic"]
updated = False

if os.path.exists(CSV_PATH):
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["filename"] == filename:
                row["category"]   = category
                row["topic"]      = topic
                row["summary"]    = summary
                row["key_people"] = key_people
                updated = True
            rows.append(row)

if not updated:
    rows.append({
        "filename":   filename,
        "date":       recorded_at,
        "category":   category,
        "summary":    summary,
        "key_people": key_people,
        "topic":      topic
    })

import tempfile
tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(CSV_PATH), suffix=".csv")
try:
    with os.fdopen(tmp_fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, CSV_PATH)  # atomic rename
except Exception:
    os.unlink(tmp_path)
    raise

print(f"  CSV updated: {CSV_PATH}")
