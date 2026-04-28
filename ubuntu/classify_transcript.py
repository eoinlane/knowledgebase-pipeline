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
    from shared.config import OLLAMA_URL, MODEL
except ImportError:
    OLLAMA_URL = "http://192.168.0.70:11434/api/chat"
    MODEL = "qwen2.5:14b"

LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_MODEL = "claude-haiku-4-5"

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

SYSTEM_PROMPT = """You are an AI assistant that classifies meeting transcripts for Eoin Lane, an AI consultant based in Dublin.

CATEGORIES (pick exactly one):
- NTA       — National Transport Authority. Eoin and Cathal are Org Group advisors to NTA, reporting to Declan Sheehan (CTO).
- DCC       — Dublin City Council. AI strategy, Gen AI Lab, Building Control/DAC, ADAPT partnership.
- Diotima   — Small AI company at Trinity. Key people: Siobhan Ryan (co-founder), Jonathan (co-founder), Masa/Mahsa (ML engineer).
- ADAPT     — ADAPT Research Centre embedded at DCC. Key people: Declan (lead), Kaiser/Kizzer (researcher), Ashish (head).
- TBS       — Trinity Business School. Eoin as adjunct lecturer, executive programmes.
- Paradigm  — Fintech/banking AI startup. Key people: Guy (architect), Arjit/Arjun (engineering), Sarah (commercial).
- other:blank    — Empty recording, one-word fragments, accidental recording, no meaningful content.
- other:personal — Personal matters (e.g. Swiss legal case with Laurent).
- other:conference — Conference or external event recordings.
- other:lgma — LGMA (Local Government Management Agency) recordings.

DISAMBIGUATION RULES:
- "Cathal" or "Cathal Bellew" alone (no DCC-specific people present) → NTA. Cathal Bellew is an Org Group advisor to NTA. Meetings with him about brown bags, governance, planning, project updates = NTA even if DCC topics are discussed.
- "Siobhan" alone: if context is EdTech/ethics/Diotima platform → Diotima. If context is NTA/transport/governance → NTA.
- "Jonathan" or "Masa"/"Mahsa" mentioned → Diotima.
- CAD drawings / Part M / Building Control / Disability Access Certificate (DAC) → DCC.
- ANY spelling variant of "Eoin Lane" is the recorder. Common WhisperX mishearings: "Owen Lane", "Eoghan Lane", "Owen Layne". Always normalise to "Eoin Lane".
- "Cahal" / "Carla" / "Cahill" / "Cottle" / "Karl Bellew" → Cathal Bellew (NTA). "NCA" = NTA.
- Neil (Org Group London, Advisory Services head) and Mark (Org Group commercial) are NTA-related contacts — calls with them about the NTA engagement → NTA.
- Morgan McKinley / Org Group discussions about placing Eoin at NTA → NTA.
- Introductory or business development calls about NTA → NTA.
- other:blank ONLY for truly empty, silent, inaudible, or single-word/fragment recordings with no real content.
- other:personal = any personal content: consumer tech discussions, family, legal matters, personal reviews, non-work topics. Do NOT use other:blank for recordings with actual conversation just because the topic is personal.
- Welsh or Korean text in what should be an English recording → likely other:blank.

OUTPUT: Respond with ONLY a JSON object, no explanation, no markdown, no <think> tags:
{
  "category": "<one of the categories above>",
  "topic": "<short topic label, e.g. 'Use Case Discovery' or 'Building Control / DAC'>",
  "summary": "<2-3 sentence summary of what was discussed>",
  "key_people": "<comma-separated list of names mentioned>"
}"""

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

category   = classification.get("category", "other:blank")
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
