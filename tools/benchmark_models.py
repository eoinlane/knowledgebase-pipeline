#!/usr/bin/env python3
"""
Benchmark Ollama models for classification and speaker ID tasks.
Measures speed and quality against ground truth (existing CSV classifications).

Usage:
    python3 benchmark_models.py --model deepseek-r1:14b
    python3 benchmark_models.py --model qwen2.5:14b
    python3 benchmark_models.py --model qwen2.5:14b --tasks classify
"""

import argparse, json, os, re, subprocess, sys, time, urllib.request
from datetime import datetime

OLLAMA_URL_DEFAULT = "http://192.168.0.70:11434/api/chat"
UBUNTU_HOST = "eoin@nvidiaubuntubox"
TRANSCRIPT_CACHE = "/tmp/benchmark_transcripts"

# 8 curated transcripts covering size and category diversity
BENCHMARK_SET = [
    {"uuid": "05377C97-45F9-4ACA-B2A9-04EE6AAADCE7", "lines": 6,
     "category": "other:blank", "key_people": "", "topic": "",
     "label": "blank-short"},
    {"uuid": "423513AC-098B-4BA4-B2FE-42D7669A87BF", "lines": 26,
     "category": "Paradigm", "key_people": "", "topic": "Share Structure & Roadmap",
     "label": "paradigm-short"},
    {"uuid": "51D5BF4B-097B-4342-9FB9-FAD4A1525C06", "lines": 82,
     "category": "DCC", "key_people": "Declan, Alex, Jonathan", "topic": "DCC: AI Strategy & Governance Meeting",
     "label": "dcc-medium"},
    {"uuid": "29CB537F-7D94-4C09-B3C9-F2BB146B3D6A", "lines": 115,
     "category": "Diotima", "key_people": "Masa, Siobhan, Long, Carl", "topic": "EdTech Platform: AI Question & Rubric Generation",
     "label": "diotima-medium"},
    {"uuid": "F1E4B5AA-26BE-4692-9D82-7EF1BE1A6A6A", "lines": 109,
     "category": "NTA", "key_people": "Declan, Alex, Eoin Lane, Fiona", "topic": "Taxi/SPSV",
     "label": "nta-medium"},
    {"uuid": "19A83FCE-461B-41E6-9D24-6E0EEDE1B7E7", "lines": 103,
     "category": "ADAPT", "key_people": "Declan", "topic": "DCC AI Lab: Microsoft Hackathon & AI Awareness Event",
     "label": "adapt-medium"},
    {"uuid": "FB47B0DF-21F5-4447-A539-7E4C5D405AFF", "lines": 402,
     "category": "DCC", "key_people": "SPEAKER_00, SPEAKER_01, SPEAKER_02", "topic": "AI Strategy and Proof of Concepts",
     "label": "dcc-long"},
    {"uuid": "B33E5A54-3E93-4168-8E6E-DBAF4C2284D1", "lines": 418,
     "category": "other:personal", "key_people": "Conor, Chris, Steven", "topic": "Personal Reflections on Relocation and Parenting",
     "label": "personal-long"},
]

CLASSIFY_SYSTEM_PROMPT = """You are an AI assistant that classifies meeting transcripts for Eoin Lane, an AI consultant based in Dublin.

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
- "Siobhan" alone: if context is EdTech/ethics/Diotima platform → Diotima. If context is NTA/transport/governance → NTA.
- "Jonathan" or "Masa"/"Mahsa" mentioned → Diotima.
- CAD drawings / Part M / Building Control / Disability Access Certificate (DAC) → DCC.
- "Owen Lane" = Eoin Lane (transcription error). "Cahal" = Cathal (transcription error). "NCA" = NTA (transcription error).
- Neil (Org Group London, Advisory Services head) and Mark (Org Group commercial) are NTA-related contacts — calls with them about the NTA engagement → NTA.
- Morgan McKinley / Org Group discussions about placing Eoin at NTA → NTA.
- other:blank ONLY for truly empty, silent, inaudible, or single-word/fragment recordings with no real content.
- other:personal = any personal content: consumer tech discussions, family, legal matters, personal reviews, non-work topics.
- Welsh or Korean text in what should be an English recording → likely other:blank.

OUTPUT: Respond with ONLY a JSON object, no explanation, no markdown, no <think> tags:
{
  "category": "<one of the categories above>",
  "topic": "<short topic label>",
  "summary": "<2-3 sentence summary of what was discussed>",
  "key_people": "<comma-separated list of names mentioned>"
}"""

SPEAKER_ID_SYSTEM_PROMPT = """You identify speakers in meeting transcripts. Given a transcript with [SPEAKER_XX] labels and a list of likely attendees, map each speaker label to a real name.

OUTPUT: Respond with ONLY a JSON object mapping speaker labels to names:
{
  "SPEAKER_00": {"name": "Person Name", "confidence": "high/medium/low"},
  "SPEAKER_01": {"name": "Person Name", "confidence": "high/medium/low"}
}

Rules:
- SPEAKER_00 is usually Eoin Lane (the recorder)
- Use attendee list and context clues (who addresses whom by name)
- "high" = very confident, "medium" = likely, "low" = guess
- If unknown, use {"name": null, "confidence": "low"}"""


def fetch_transcripts():
    """Fetch benchmark transcripts from Ubuntu, cache locally."""
    os.makedirs(TRANSCRIPT_CACHE, exist_ok=True)
    for item in BENCHMARK_SET:
        local = os.path.join(TRANSCRIPT_CACHE, item["uuid"] + ".txt")
        if os.path.exists(local) and os.path.getsize(local) > 0:
            continue
        remote = f"{UBUNTU_HOST}:~/audio-inbox/Transcriptions/{item['uuid']}.txt"
        r = subprocess.run(["scp", remote, local], capture_output=True, timeout=30)
        if r.returncode != 0:
            print(f"  WARN: could not fetch {item['uuid']}: {r.stderr.decode()[:80]}")


def read_transcript(uuid):
    path = os.path.join(TRANSCRIPT_CACHE, uuid + ".txt")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read()


def call_ollama(model, messages, ollama_url):
    """POST to Ollama, return (response_dict, wall_time_seconds)."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0}
    }).encode()

    req = urllib.request.Request(
        ollama_url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read())
        wall = time.monotonic() - t0
        return result, wall
    except Exception as e:
        wall = time.monotonic() - t0
        return {"error": str(e)}, wall


def parse_response(raw_content):
    """Strip <think> blocks, extract JSON."""
    clean = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
    # Count think overhead
    think_matches = re.findall(r"<think>(.*?)</think>", raw_content, re.DOTALL)
    think_chars = sum(len(t) for t in think_matches)

    jm = re.search(r"\{.*\}", clean, re.DOTALL)
    parsed = None
    if jm:
        try:
            parsed = json.loads(jm.group())
        except json.JSONDecodeError:
            pass
    return parsed, think_chars, len(clean)


def benchmark_classify(model, transcript_text, ollama_url):
    content = transcript_text[:6000]
    messages = [
        {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
        {"role": "user", "content": f"Classify this transcript:\n\n{content}"}
    ]
    result, wall = call_ollama(model, messages, ollama_url)
    if "error" in result:
        return {"task": "classify", "success": False, "error": result["error"], "wall_time": wall}

    raw = result.get("message", {}).get("content", "")
    parsed, think_chars, clean_chars = parse_response(raw)
    eval_count = result.get("eval_count", 0)
    eval_dur = result.get("eval_duration", 1) / 1e9

    return {
        "task": "classify",
        "success": parsed is not None and "category" in (parsed or {}),
        "wall_time": round(wall, 1),
        "eval_count": eval_count,
        "eval_duration": round(eval_dur, 1),
        "tok_per_sec": round(eval_count / eval_dur, 1) if eval_dur > 0 else 0,
        "think_overhead_pct": round(think_chars * 100 / max(len(raw), 1)),
        "prompt_eval_count": result.get("prompt_eval_count", 0),
        "load_duration": round(result.get("load_duration", 0) / 1e9, 2),
        "output": parsed,
    }


def benchmark_speaker_id(model, transcript_text, key_people, ollama_url):
    # Extract speaker labels from transcript
    speakers = sorted(set(re.findall(r'\[SPEAKER_\d+\]', transcript_text)))
    if not speakers:
        return {"task": "speaker_id", "success": True, "skipped": True,
                "reason": "no SPEAKER labels", "wall_time": 0}

    attendee_hint = f"Likely attendees: {key_people}" if key_people else "No attendee information available."
    sample = transcript_text[:4000]

    messages = [
        {"role": "system", "content": SPEAKER_ID_SYSTEM_PROMPT},
        {"role": "user", "content": f"{attendee_hint}\n\nTranscript:\n{sample}"}
    ]
    result, wall = call_ollama(model, messages, ollama_url)
    if "error" in result:
        return {"task": "speaker_id", "success": False, "error": result["error"], "wall_time": wall}

    raw = result.get("message", {}).get("content", "")
    parsed, think_chars, clean_chars = parse_response(raw)
    eval_count = result.get("eval_count", 0)
    eval_dur = result.get("eval_duration", 1) / 1e9

    # Quality checks
    eoin_detected = False
    names_plausible = False
    if parsed:
        for label, info in parsed.items():
            if isinstance(info, dict):
                name = info.get("name", "")
                if name and "eoin" in str(name).lower():
                    eoin_detected = True
        names_plausible = any(
            isinstance(v, dict) and v.get("name") for v in parsed.values()
        )

    return {
        "task": "speaker_id",
        "success": parsed is not None,
        "wall_time": round(wall, 1),
        "eval_count": eval_count,
        "eval_duration": round(eval_dur, 1),
        "tok_per_sec": round(eval_count / eval_dur, 1) if eval_dur > 0 else 0,
        "think_overhead_pct": round(think_chars * 100 / max(len(raw), 1)),
        "speaker_count": len(speakers),
        "eoin_detected": eoin_detected,
        "names_plausible": names_plausible,
        "output": parsed,
    }


def category_match(predicted, ground_truth):
    """Compare categories: exact, close (both other:*), or mismatch."""
    if not predicted:
        return "fail"
    p, g = predicted.lower().strip(), ground_truth.lower().strip()
    if p == g:
        return "exact"
    if p.startswith("other:") and g.startswith("other:"):
        return "close"
    return "mismatch"


def key_people_overlap(predicted_str, ground_truth_str):
    """Jaccard-ish overlap of key people names."""
    def names(s):
        return {n.strip().lower() for n in re.split(r'[,;]', s) if n.strip() and n.strip() != "SPEAKER_00"}
    pred = names(predicted_str or "")
    truth = names(ground_truth_str or "")
    if not truth:
        return None  # can't measure
    if not pred:
        return 0.0
    return round(len(pred & truth) / len(pred | truth), 2)


def print_results(model, results):
    print(f"\n{'='*80}")
    print(f"BENCHMARK RESULTS: {model}")
    print(f"{'='*80}\n")

    # Classification table
    classify_results = [r for r in results if r["task"] == "classify"]
    if classify_results:
        print("## Classification\n")
        print(f"{'Label':<20} {'Cat Match':<10} {'Wall(s)':<8} {'Tokens':<8} {'tok/s':<7} {'Think%':<7} {'Parse':<6}")
        print("-" * 70)
        for r in classify_results:
            label = r.get("label", "?")
            cat = r.get("cat_match", "?")
            wall = r.get("wall_time", 0)
            tok = r.get("eval_count", 0)
            tps = r.get("tok_per_sec", 0)
            think = r.get("think_overhead_pct", 0)
            ok = "OK" if r.get("success") else "FAIL"
            print(f"{label:<20} {cat:<10} {wall:<8} {tok:<8} {tps:<7} {think:<7} {ok:<6}")

        # Aggregates
        exact = sum(1 for r in classify_results if r.get("cat_match") == "exact")
        close = sum(1 for r in classify_results if r.get("cat_match") == "close")
        total = len(classify_results)
        avg_wall = sum(r.get("wall_time", 0) for r in classify_results) / max(total, 1)
        avg_tok = sum(r.get("eval_count", 0) for r in classify_results) / max(total, 1)
        avg_think = sum(r.get("think_overhead_pct", 0) for r in classify_results) / max(total, 1)
        parse_ok = sum(1 for r in classify_results if r.get("success"))

        print(f"\nCategory accuracy: {exact}/{total} exact, {close}/{total} close, {total-exact-close}/{total} mismatch")
        print(f"Parse success: {parse_ok}/{total}")
        print(f"Avg wall time: {avg_wall:.1f}s | Avg tokens: {avg_tok:.0f} | Avg think overhead: {avg_think:.0f}%")

    # Speaker ID table
    sid_results = [r for r in results if r["task"] == "speaker_id" and not r.get("skipped")]
    if sid_results:
        print(f"\n## Speaker ID\n")
        print(f"{'Label':<20} {'Wall(s)':<8} {'Tokens':<8} {'tok/s':<7} {'Think%':<7} {'Eoin?':<6} {'Parse':<6}")
        print("-" * 66)
        for r in sid_results:
            label = r.get("label", "?")
            wall = r.get("wall_time", 0)
            tok = r.get("eval_count", 0)
            tps = r.get("tok_per_sec", 0)
            think = r.get("think_overhead_pct", 0)
            eoin = "Yes" if r.get("eoin_detected") else "No"
            ok = "OK" if r.get("success") else "FAIL"
            print(f"{label:<20} {wall:<8} {tok:<8} {tps:<7} {think:<7} {eoin:<6} {ok:<6}")

        avg_wall = sum(r.get("wall_time", 0) for r in sid_results) / max(len(sid_results), 1)
        eoin_rate = sum(1 for r in sid_results if r.get("eoin_detected")) / max(len(sid_results), 1)
        print(f"\nAvg wall time: {avg_wall:.1f}s | Eoin detection: {eoin_rate:.0%}")

    # Disagreements
    mismatches = [r for r in classify_results if r.get("cat_match") == "mismatch"]
    if mismatches:
        print(f"\n## Category Disagreements\n")
        for r in mismatches:
            pred_cat = r.get("output", {}).get("category", "?") if r.get("output") else "?"
            print(f"  {r['label']}: predicted={pred_cat}, ground_truth={r['ground_truth_cat']}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark Ollama models for pipeline tasks")
    parser.add_argument("--model", required=True, help="Model name (e.g. deepseek-r1:14b, qwen2.5:14b)")
    parser.add_argument("--ollama-url", default=OLLAMA_URL_DEFAULT, help="Ollama API URL")
    parser.add_argument("--tasks", default="classify,speaker_id", help="Comma-separated tasks to benchmark")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetching transcripts (use cache)")
    args = parser.parse_args()

    tasks = [t.strip() for t in args.tasks.split(",")]

    print(f"Benchmarking: {args.model}")
    print(f"Ollama URL:   {args.ollama_url}")
    print(f"Tasks:        {', '.join(tasks)}")
    print(f"Transcripts:  {len(BENCHMARK_SET)}")

    # Fetch transcripts
    if not args.no_fetch:
        print("\nFetching transcripts from Ubuntu...")
        fetch_transcripts()

    # Warm up model
    print(f"\nWarming up {args.model}...")
    call_ollama(args.model, [{"role": "user", "content": "Say OK"}], args.ollama_url)

    results = []
    for i, item in enumerate(BENCHMARK_SET, 1):
        transcript = read_transcript(item["uuid"])
        if not transcript:
            print(f"  [{i}/{len(BENCHMARK_SET)}] SKIP {item['label']} — transcript not found")
            continue

        print(f"  [{i}/{len(BENCHMARK_SET)}] {item['label']} ({item['lines']}L)...", end="", flush=True)

        if "classify" in tasks:
            r = benchmark_classify(args.model, transcript, args.ollama_url)
            r["label"] = item["label"]
            r["uuid"] = item["uuid"]
            r["ground_truth_cat"] = item["category"]
            if r.get("output") and isinstance(r["output"], dict):
                r["cat_match"] = category_match(r["output"].get("category"), item["category"])
                r["kp_overlap"] = key_people_overlap(
                    r["output"].get("key_people", ""), item["key_people"])
            else:
                r["cat_match"] = "fail"
            results.append(r)
            print(f" classify={r['wall_time']}s", end="", flush=True)

        if "speaker_id" in tasks:
            r = benchmark_speaker_id(args.model, transcript, item["key_people"], args.ollama_url)
            r["label"] = item["label"]
            r["uuid"] = item["uuid"]
            results.append(r)
            if not r.get("skipped"):
                print(f" speaker_id={r['wall_time']}s", end="", flush=True)

        print()

    print_results(args.model, results)

    # Save JSON
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_slug = args.model.replace(":", "_").replace("/", "_")
    out_file = f"benchmark_results_{model_slug}_{ts}.json"
    with open(out_file, "w") as f:
        json.dump({"model": args.model, "timestamp": ts, "results": results}, f, indent=2, default=str)
    print(f"\nResults saved to {out_file}")


if __name__ == "__main__":
    main()
