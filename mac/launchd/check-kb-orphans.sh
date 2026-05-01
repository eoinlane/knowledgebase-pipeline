#!/bin/bash
# Sanity check that the orphan-cleanup block in build_knowledge_base.py is
# doing its job. Logs to ~/.local/bin/check-kb-orphans.log; only emits a
# noisy line when orphans are present.
#
# Detects KB markdowns that share a `source_file:` frontmatter UUID — if any
# UUID appears in 2+ files, the build's orphan cleanup didn't fire.

LOG="$HOME/.local/bin/check-kb-orphans.log"
KB_DIR="$HOME/knowledge_base/meetings"

count=$(/usr/local/bin/python3 - "$KB_DIR" <<'PY'
import sys, os, re
from collections import defaultdict
kb = sys.argv[1]
d = defaultdict(list)
for f in os.listdir(kb):
    if not f.endswith(".md"):
        continue
    with open(os.path.join(kb, f), errors="replace") as fh:
        text = fh.read(4096)
    m = re.search(r"^source_file:\s*(\S+)", text, re.MULTILINE)
    if m:
        d[m.group(1)].append(f)
orphans = sum(len(v) - 1 for v in d.values() if len(v) > 1)
print(orphans)
if orphans:
    sys.stderr.write("Offending UUIDs:\n")
    for u, files in d.items():
        if len(files) > 1:
            sys.stderr.write(f"  {u}: {len(files)} files: {files[:3]}\n")
PY
)

ts=$(date "+%Y-%m-%d %H:%M:%S")
if [ "$count" -eq 0 ]; then
    echo "$ts: OK — 0 orphans" >> "$LOG"
else
    echo "$ts: WARNING — $count orphan(s) detected" >> "$LOG"
fi
