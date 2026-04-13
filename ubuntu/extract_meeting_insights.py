#!/usr/bin/env python3
"""
Extract structured insights from a meeting transcript using qwen2.5:14b.
Runs after classification and speaker ID. Saves insights as JSON alongside
the transcript for the KB build to consume.

Usage: python3 extract_meeting_insights.py <transcript_txt> <csv_path>
       python3 extract_meeting_insights.py --batch <csv_path>  # process all unextracted
"""

import sys, os, json, re, csv, time
import urllib.request

PIPELINE_DIR = os.environ.get("PIPELINE_DIR", os.path.expanduser("~/knowledgebase-pipeline"))
if os.path.isdir(PIPELINE_DIR) and PIPELINE_DIR not in sys.path:
    sys.path.insert(0, PIPELINE_DIR)
try:
    from shared.config import OLLAMA_URL, MODEL
except ImportError:
    OLLAMA_URL = "http://192.168.0.70:11434/api/chat"
    MODEL = "qwen2.5:14b"

# LiteLLM proxy for Claude Haiku (200K context, better extraction quality)
LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_MODEL = "claude-haiku-4-5"
USE_LITELLM = True  # Use Haiku for extraction, fall back to Ollama if unavailable
INSIGHTS_DIR = os.path.expanduser("~/audio-inbox/Insights")
os.makedirs(INSIGHTS_DIR, exist_ok=True)

SYSTEM_PROMPT = """You extract structured insights from meeting transcripts. The transcript has speaker labels in square brackets — either real names like [Eoin Lane] or anonymous labels like [SPEAKER_00].

When the transcript uses SPEAKER_XX labels, use the MEETING PARTICIPANTS list provided to figure out who is who based on context (what they say, their role, what others call them). Assign action items to real participant names, not SPEAKER_XX labels. If you truly cannot determine who a speaker is, use "unknown".

Extract the following. Be specific — include names, dates, and concrete details. If a field has no items, return an empty list.

OUTPUT: Respond with ONLY a JSON object:
{
  "action_items": [
    {"owner": "Person Name (real name, not SPEAKER_XX)", "action": "what they committed to do", "deadline": "by when, or null"}
  ],
  "decisions": [
    "Specific decision that was made or agreed"
  ],
  "follow_ups": [
    {"description": "what needs to happen next", "who": "person responsible, or null"}
  ],
  "open_questions": [
    "Unresolved question or issue raised but not answered"
  ],
  "key_topics": [
    "Specific topic discussed (more granular than meeting title)"
  ]
}

Rules:
- Action items must have a clear owner — use real names from the participants list, not SPEAKER_XX
- Decisions are things that were agreed or concluded, not just discussed
- Follow-ups are next steps that weren't assigned to a specific person yet
- Open questions are things explicitly flagged as needing answers
- Key topics should be 3-7 specific items, not generic labels
- One speaker is always Eoin Lane (the meeting recorder) — usually the one asking questions or facilitating
- If the transcript is too short or has no substantive content, return empty lists"""


def call_litellm(messages):
    """Call Claude Haiku via LiteLLM proxy. 200K context, better JSON output."""
    payload = json.dumps({
        "model": LITELLM_MODEL,
        "messages": messages,
        "temperature": 0,
    }).encode()

    req = urllib.request.Request(
        LITELLM_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    # OpenAI-compatible response format
    return {"message": result["choices"][0]["message"]}


def call_ollama(messages):
    """Call Ollama (fallback if LiteLLM unavailable)."""
    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0}
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


def call_llm(messages):
    """Call LiteLLM (Haiku) if available, fall back to Ollama."""
    if USE_LITELLM:
        try:
            return call_litellm(messages)
        except Exception as e:
            print(f"  LiteLLM unavailable ({e}), falling back to Ollama")
    return call_ollama(messages)


def extract_insights(transcript_text, participants=None, category="", topic=""):
    """Send transcript to LLM for insight extraction.
    Haiku (via LiteLLM): sends full transcript (200K context).
    Ollama fallback: caps at 24K chars (32K context limit)."""
    if USE_LITELLM:
        content = transcript_text  # Haiku handles 200K context — send everything
    else:
        content = transcript_text[:24000]

    # Build participant context for the LLM
    context_parts = []
    if category:
        context_parts.append(f"Meeting category: {category}")
    if topic:
        context_parts.append(f"Meeting topic: {topic}")
    if participants:
        context_parts.append(f"Meeting participants: {', '.join(participants)}")
    else:
        context_parts.append("Meeting participants: unknown — use transcript context clues to identify speakers")

    context_header = "\n".join(context_parts)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{context_header}\n\nExtract insights from this meeting transcript:\n\n{content}"}
    ]

    result = call_llm(messages)
    raw = result["message"]["content"].strip()

    # Strip any <think> tags (in case model is swapped to a reasoning model)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    # Extract JSON
    jm = re.search(r"\{.*\}", raw, re.DOTALL)
    if jm:
        try:
            return json.loads(jm.group())
        except json.JSONDecodeError:
            pass

    # Retry with forceful JSON instruction
    messages.append({"role": "assistant", "content": raw[:200]})
    messages.append({"role": "user", "content": "Reformat your response as ONLY a JSON object with keys: action_items, decisions, follow_ups, open_questions, key_topics. No prose."})

    result = call_llm(messages)
    raw2 = result["message"]["content"].strip()
    raw2 = re.sub(r"<think>.*?</think>", "", raw2, flags=re.DOTALL).strip()
    jm2 = re.search(r"\{.*\}", raw2, re.DOTALL)
    if jm2:
        try:
            return json.loads(jm2.group())
        except json.JSONDecodeError:
            pass

    return None


def process_transcript(txt_path, csv_path, force=False):
    """Extract insights for a single transcript."""
    with open(txt_path) as f:
        content = f.read()

    # Extract UUID
    uuid = ""
    for line in content.splitlines()[:3]:
        if line.startswith("File:"):
            uuid = line.replace("File:", "").strip()
            uuid = re.sub(r'\.(m4a|txt)$', '', uuid)

    if not uuid:
        print(f"  No UUID found in {txt_path}", file=sys.stderr)
        return False

    insights_file = os.path.join(INSIGHTS_DIR, uuid + ".json")

    # Skip if already extracted (unless force)
    if os.path.exists(insights_file) and not force:
        print(f"  Already extracted: {uuid}")
        return True

    # Get category, topic, and key_people from CSV
    category = ""
    topic = ""
    key_people = []
    if csv_path and os.path.exists(csv_path):
        with open(csv_path) as f:
            for row in csv.reader(f):
                if len(row) >= 6 and uuid in row[0]:
                    category = row[2]
                    topic = row[5]
                    # key_people is column 4 (semicolon or comma separated)
                    if row[4].strip():
                        key_people = [p.strip() for p in re.split(r"[;,]", row[4]) if p.strip()]
                    break
    # Always include Eoin Lane as a participant
    if "Eoin Lane" not in key_people and "Eoin" not in key_people:
        key_people.insert(0, "Eoin Lane")

    # Skip blank/minimal recordings
    lines = [l for l in content.splitlines() if l.strip() and not l.startswith(("File:", "Recorded:", "---"))]
    if len(lines) < 5:
        print(f"  Skipping {uuid} — too short ({len(lines)} lines)")
        # Save empty insights so we don't retry
        with open(insights_file, "w") as f:
            json.dump({"skipped": True, "reason": "too_short"}, f)
        return True

    print(f"  Extracting: {uuid} ({len(lines)} lines, {category})...")
    t0 = time.time()

    try:
        insights = extract_insights(content, participants=key_people, category=category, topic=topic)
    except Exception as e:
        print(f"  FAILED: {e}", file=sys.stderr)
        return False

    if not insights:
        print(f"  FAILED: could not parse JSON response")
        return False

    # Add metadata
    insights["uuid"] = uuid
    insights["category"] = category
    insights["topic"] = topic
    insights["extracted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    insights["model"] = LITELLM_MODEL if USE_LITELLM else MODEL

    # Count items
    n_actions = len(insights.get("action_items", []))
    n_decisions = len(insights.get("decisions", []))
    n_followups = len(insights.get("follow_ups", []))
    n_topics = len(insights.get("key_topics", []))

    elapsed = time.time() - t0
    print(f"  OK ({elapsed:.1f}s) — {n_actions} actions, {n_decisions} decisions, {n_followups} follow-ups, {n_topics} topics")

    with open(insights_file, "w") as f:
        json.dump(insights, f, indent=2)

    return True


def batch_process(csv_path, limit=None, force=False):
    """Process all transcripts that don't have insights yet."""
    trans_dir = os.path.expanduser("~/audio-inbox/Transcriptions")

    # Get list of transcripts with classification (skip unclassified)
    classified = set()
    with open(csv_path) as f:
        for row in csv.reader(f):
            if len(row) >= 6 and row[2] != "other:blank":
                uuid = row[0].replace(".txt", "")
                classified.add(uuid)

    # Find transcripts needing extraction
    to_process = []
    for fname in sorted(os.listdir(trans_dir)):
        if not fname.endswith(".txt"):
            continue
        uuid = fname.replace(".txt", "")
        if uuid not in classified:
            continue
        insights_file = os.path.join(INSIGHTS_DIR, uuid + ".json")
        if os.path.exists(insights_file) and not force:
            continue
        to_process.append(os.path.join(trans_dir, fname))

    print(f"Batch: {len(to_process)} transcripts to process")
    if limit:
        to_process = to_process[:limit]
        print(f"  Limited to {limit}")

    ok, failed = 0, 0
    for i, path in enumerate(to_process, 1):
        print(f"[{i}/{len(to_process)}]", end="")
        if process_transcript(path, csv_path, force=force):
            ok += 1
        else:
            failed += 1

    print(f"\nBatch complete: {ok} OK, {failed} failed")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 extract_meeting_insights.py <transcript.txt> <csv_path>")
        print("       python3 extract_meeting_insights.py --batch <csv_path> [--limit N] [--force]")
        sys.exit(1)

    if sys.argv[1] == "--batch":
        csv_path = sys.argv[2]
        limit = None
        force = False
        for i, arg in enumerate(sys.argv[3:]):
            if arg == "--limit" and i + 4 < len(sys.argv):
                limit = int(sys.argv[i + 4])
            if arg == "--force":
                force = True
        batch_process(csv_path, limit=limit, force=force)
    else:
        txt_path = sys.argv[1]
        csv_path = sys.argv[2]
        if process_transcript(txt_path, csv_path):
            sys.exit(0)
        else:
            sys.exit(1)
