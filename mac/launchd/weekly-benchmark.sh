#!/bin/bash
# Weekly model regression check. Runs the 8-transcript benchmark suite for
# qwen2.5:14b (production classify primary) and claude-haiku-4-5 (insights
# primary), then diffs against the previous run for each model.
#
# On regression — exact-accuracy drops OR avg wall time grows >25% — emails
# an alert via the existing morning_brief_emailer.py path. On clean run,
# silent; the log file is the audit trail.
#
# Triggered by launchd Sunday 02:00 IST (before the 04:00 nightly rebuild,
# so the benchmark + rebuild can't collide on ollama-box).

LOG="/Users/eoin/.local/bin/weekly-benchmark.log"
RESULTS_DIR="/Users/eoin/knowledgebase-pipeline/benchmark_results"
REPO_DIR="/Users/eoin/knowledgebase-pipeline"
EMAILER="/Users/eoin/morning_brief_emailer.py"
REPORT="/Users/eoin/weekly_benchmark_report.md"

echo "$(date): === Weekly benchmark START ===" >> "$LOG"

cd "$REPO_DIR" || { echo "$(date): cd failed" >> "$LOG"; exit 1; }

# Models to score weekly. qwen14b is the production classify primary; haiku
# is the insights primary. Both must stay healthy.
for spec in "ollama:qwen2.5:14b" "haiku:claude-haiku-4-5"; do
    provider="${spec%%:*}"
    model="${spec#*:}"
    echo "$(date): Running benchmark for $provider/$model..." >> "$LOG"
    if [ "$provider" = "ollama" ]; then
        /usr/local/bin/python3 tools/benchmark_models.py --model "$model" >> "$LOG" 2>&1
    else
        /usr/local/bin/python3 tools/benchmark_models.py --provider haiku --tasks classify --no-fetch >> "$LOG" 2>&1
    fi
done

# Move any results that landed in the repo root into benchmark_results/
mv "$REPO_DIR"/benchmark_results_*.json "$RESULTS_DIR/" 2>/dev/null

# Diff vs previous run for each model and assemble the report (truncate first)
> "$REPORT"
/usr/local/bin/python3 - >> "$REPORT" 2>>"$LOG" <<'PY'
"""Diff two most-recent benchmark JSON files per model. Emit a markdown
report; if any model regressed, print a 'REGRESSION' marker that the wrapper
detects and uses to decide whether to email."""
import datetime as dt
import glob
import json
import os

RESULTS_DIR = "/Users/eoin/knowledgebase-pipeline/benchmark_results"
REGRESSION_WALL_PCT = 25  # >+25% on avg wall time triggers an alert
MODELS = ["qwen2.5_14b", "claude-haiku-4-5"]


def stats(path):
    with open(path) as f:
        data = json.load(f)
    classify = [r for r in data["results"] if r.get("task") == "classify"]
    n = len(classify) or 1
    exact = sum(1 for r in classify if r.get("cat_match") == "exact")
    parse_ok = sum(1 for r in classify if r.get("success"))
    avg_wall = sum(float(r.get("wall_time", 0)) for r in classify) / n
    return {"exact": exact, "n": n, "parse_ok": parse_ok, "avg_wall": avg_wall}


regression = False
print(f"---")
print(f"title: \"Weekly Benchmark {dt.date.today().isoformat()}\"")
print(f"date: {dt.date.today().isoformat()}")
print(f"type: weekly_benchmark")
print(f"---\n")
print(f"# Weekly Benchmark — {dt.date.today().strftime('%A %d %B %Y')}\n")

for model_slug in MODELS:
    files = sorted(glob.glob(f"{RESULTS_DIR}/benchmark_results_{model_slug}_*.json"))
    if len(files) < 2:
        print(f"## {model_slug}\n\n_Insufficient history (need 2+ runs)._\n")
        continue
    new, prev = files[-1], files[-2]
    n_s, p_s = stats(new), stats(prev)
    exact_delta = n_s["exact"] - p_s["exact"]
    wall_pct = (n_s["avg_wall"] - p_s["avg_wall"]) / max(p_s["avg_wall"], 0.001) * 100
    flag = ""
    if exact_delta < 0:
        flag = " ⚠️ ACCURACY REGRESSION"
        regression = True
    elif wall_pct > REGRESSION_WALL_PCT:
        flag = f" ⚠️ LATENCY REGRESSION (+{wall_pct:.0f}%)"
        regression = True
    print(f"## {model_slug}{flag}\n")
    print(f"| Metric | Previous | Current | Delta |")
    print(f"|---|---|---|---|")
    print(f"| Exact accuracy | {p_s['exact']}/{p_s['n']} | {n_s['exact']}/{n_s['n']} | {exact_delta:+d} |")
    print(f"| Parse success | {p_s['parse_ok']}/{p_s['n']} | {n_s['parse_ok']}/{n_s['n']} | {n_s['parse_ok']-p_s['parse_ok']:+d} |")
    print(f"| Avg wall (s) | {p_s['avg_wall']:.1f} | {n_s['avg_wall']:.1f} | {wall_pct:+.0f}% |")
    print(f"| Previous run | `{os.path.basename(prev)}` |")
    print(f"| Current run | `{os.path.basename(new)}` |")
    print()

if regression:
    print("\nREGRESSION_DETECTED\n")
PY

# Email only on regression
if grep -q "REGRESSION_DETECTED" "$REPORT"; then
    echo "$(date): Regression detected — sending alert email" >> "$LOG"
    /usr/local/bin/python3 "$EMAILER" --file "$REPORT" --subject "Benchmark Regression" >> "$LOG" 2>&1
else
    echo "$(date): Clean run — no email" >> "$LOG"
fi

echo "$(date): === Weekly benchmark END ===" >> "$LOG"
exit 0
