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

OLLAMA_URL = "http://192.168.0.70:11434/api/chat"
MODEL = "qwen2.5:14b"
INSIGHTS_DIR = os.path.expanduser("~/audio-inbox/Insights")
os.makedirs(INSIGHTS_DIR, exist_ok=True)

SYSTEM_PROMPT = """You extract structured insights from meeting transcripts. The transcript has speaker names in brackets.

Extract the following. Be specific — include names, dates, and concrete details. If a field has no items, return an empty list.

OUTPUT: Respond with ONLY a JSON object:
{
  "action_items": [
    {"owner": "Person Name", "action": "what they committed to do", "deadline": "by when, or null"}
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
- Action items must have a clear owner (the person who said "I'll..." or was asked to do something)
- Decisions are things that were agreed or concluded, not just discussed
- Follow-ups are next steps that weren't assigned to a specific person yet
- Open questions are things explicitly flagged as needing answers
- Key topics should be 3-7 specific items, not generic labels
- Use full names where known from the transcript speaker labels
- If the transcript is too short or has no substantive content, return empty lists"""


def call_ollama(messages):
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


def extract_insights(transcript_text):
    """Send full transcript to LLM for insight extraction."""
    # Cap at 12000 chars to stay within context window but get much more than classify's 6000
    content = transcript_text[:12000]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Extract insights from this meeting transcript:\n\n{content}"}
    ]

    result = call_ollama(messages)
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

    result = call_ollama(messages)
    raw2 = result["message"]["content"].strip()
    raw2 = re.sub(r"<think>.*?</think>", "", raw2, flags=re.DOTALL).strip()
    jm2 = re.search(r"\{.*\}", raw2, re.DOTALL)
    if jm2:
        try:
            return json.loads(jm2.group())
        except json.JSONDecodeError:
            pass

    return None


def process_transcript(txt_path, csv_path):
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

    # Skip if already extracted
    if os.path.exists(insights_file):
        print(f"  Already extracted: {uuid}")
        return True

    # Get category from CSV for context
    category = ""
    topic = ""
    if csv_path and os.path.exists(csv_path):
        with open(csv_path) as f:
            for row in csv.reader(f):
                if len(row) >= 6 and uuid in row[0]:
                    category = row[2]
                    topic = row[5]
                    break

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
        insights = extract_insights(content)
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
    insights["model"] = MODEL

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
        if process_transcript(path, csv_path):
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
