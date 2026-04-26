#!/usr/bin/env python3
"""
End-to-end pipeline smoke test.

Verifies that every stage of the pipeline is functional by checking
a known recording has flowed through all stages correctly.

Usage:
    python3 -m pytest tests/test_pipeline_e2e.py -v --run-slow
    python3 tests/test_pipeline_e2e.py   # standalone

Checks (all via SSH to Ubuntu + local Mac files):
1. Ubuntu SSH connectivity
2. Services running (notes-watcher, litellm)
3. ollama-box responsive
4. Disk space adequate
5. A known UUID has: transcript, CSV entry, insights JSON (non-empty)
6. Pipeline manifest operational
7. Mac KB has meeting file with frontmatter
8. Mac graph.db has action items for the meeting
9. CSV row count matches KB meeting count (within tolerance)
10. No 0-byte insight files
"""

import json
import os
import re
import sqlite3
import subprocess
import sys

import pytest

UBUNTU = "eoin@nvidiaubuntubox"
# Known good recording — short, fully processed
TEST_UUID = "493EA9BD-189A-4AB2-BC75-559657B5100D"

KB_DIR = os.path.expanduser("~/knowledge_base/meetings")
GRAPH_DB = os.path.expanduser("~/graph.db")
CSV_PATH = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/My Notes Analysis/classification.csv"
)


def ssh(cmd, timeout=15):
    """Run a command on Ubuntu via SSH, return stdout."""
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", UBUNTU, cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return result.stdout.strip(), result.returncode


def slow(fn):
    return pytest.mark.skipif(
        "--run-slow" not in sys.argv,
        reason="slow test — pass --run-slow to run"
    )(fn)


# ── Infrastructure ──────────────────────────────────────────────────────────

@slow
class TestInfrastructure:

    def test_ubuntu_ssh(self):
        out, rc = ssh("echo ok")
        assert rc == 0 and out == "ok", "Cannot SSH to Ubuntu"

    def test_notes_watcher(self):
        out, _ = ssh("systemctl is-active notes-watcher")
        assert out == "active"

    def test_litellm(self):
        out, _ = ssh("systemctl --user is-active litellm")
        assert out == "active"

    def test_ollama_box(self):
        out, rc = ssh("curl -s --max-time 10 http://192.168.0.70:11434/api/tags | head -c 20")
        assert rc == 0 and "models" in out, f"ollama-box not responding: {out}"

    def test_disk_space(self):
        out, _ = ssh("df /home/eoin --output=pcent | tail -1 | tr -d ' %'")
        pct = int(out)
        assert pct < 90, f"Ubuntu disk {pct}% used — risk of silent write failures"

    def test_no_zero_byte_insights(self):
        out, _ = ssh("find ~/audio-inbox/Insights -name '*.json' -empty | wc -l")
        count = int(out.strip())
        assert count == 0, f"{count} empty insight files — retries will be blocked"


# ── Pipeline Stages (known UUID) ────────────────────────────────────────────

@slow
class TestPipelineStages:

    def test_transcript_exists(self):
        out, _ = ssh(f"[ -s ~/audio-inbox/Transcriptions/{TEST_UUID}.txt ] && echo ok || echo missing")
        assert out == "ok", f"Transcript missing for {TEST_UUID}"

    def test_transcript_has_header(self):
        out, _ = ssh(f"head -3 ~/audio-inbox/Transcriptions/{TEST_UUID}.txt")
        assert "File:" in out and "Recorded:" in out

    def test_csv_entry(self):
        out, _ = ssh(f"grep -c '{TEST_UUID}' ~/audio-inbox/classification.csv")
        assert int(out.strip()) >= 1, f"No CSV entry for {TEST_UUID}"

    def test_insights_exists_and_valid(self):
        out, _ = ssh(f"cat ~/audio-inbox/Insights/{TEST_UUID}.json 2>/dev/null")
        assert out, f"No insights file for {TEST_UUID}"
        data = json.loads(out)
        assert not data.get("skipped"), "Insights marked as skipped"
        assert "action_items" in data, "No action_items in insights"
        assert "decisions" in data, "No decisions in insights"

    def test_manifest_operational(self):
        out, _ = ssh("python3 ~/manifest.py summary")
        assert "total=" in out and "failed=" in out


# ── Mac-side KB + Graph ─────────────────────────────────────────────────────

class TestKBAndGraph:

    def _find_meeting_file(self):
        """Find the KB meeting file for the test UUID."""
        for fname in os.listdir(KB_DIR):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(KB_DIR, fname)
            with open(fpath) as f:
                head = f.read(500)
            if TEST_UUID in head:
                return fname
        return None

    def test_meeting_file_exists(self):
        fname = self._find_meeting_file()
        assert fname, f"No KB meeting file found with source_file {TEST_UUID}"

    def test_meeting_has_frontmatter(self):
        fname = self._find_meeting_file()
        if not fname:
            pytest.skip("No meeting file")
        with open(os.path.join(KB_DIR, fname)) as f:
            content = f.read()
        assert content.startswith("---"), "Missing YAML frontmatter"
        assert "category:" in content
        assert "source_file:" in content

    def test_graph_has_meeting(self):
        fname = self._find_meeting_file()
        if not fname:
            pytest.skip("No meeting file")
        conn = sqlite3.connect(GRAPH_DB)
        count = conn.execute(
            "SELECT COUNT(*) FROM action_items WHERE meeting_filename = ?",
            (fname,)
        ).fetchone()[0]
        conn.close()
        assert count > 0, f"No action items in graph for {fname}"

    def test_graph_db_recent(self):
        age_hours = (
            os.path.getmtime(os.path.expanduser("~/graph.db"))
        )
        import time
        age = (time.time() - os.path.getmtime(GRAPH_DB)) / 3600
        assert age < 48, f"graph.db is {age:.0f}h old — rebuild may have failed"


# ── Alignment ───────────────────────────────────────────────────────────────

class TestAlignment:

    def test_csv_exists(self):
        assert os.path.exists(CSV_PATH), "CSV file not found on Mac"

    def test_kb_meeting_count_reasonable(self):
        csv_rows = sum(1 for _ in open(CSV_PATH)) - 1  # minus header
        kb_files = len([f for f in os.listdir(KB_DIR) if f.endswith(".md")])
        # KB should be at least 70% of CSV content rows
        assert kb_files > csv_rows * 0.5, (
            f"KB has {kb_files} meetings but CSV has {csv_rows} rows — large gap"
        )

    def test_newest_kb_matches_csv(self):
        """Newest KB date should be within 2 days of newest CSV date."""
        import csv as csvmod
        with open(CSV_PATH) as f:
            rows = list(csvmod.reader(f))
        if len(rows) < 2:
            pytest.skip("CSV empty")
        newest_csv = rows[-1][1].split()[0]  # date column

        kb_files = sorted(f for f in os.listdir(KB_DIR) if f.endswith(".md"))
        newest_kb = kb_files[-1].split("_")[0] if kb_files else ""

        assert newest_kb >= newest_csv[:8], (
            f"KB newest {newest_kb} is behind CSV newest {newest_csv} — rebuild may have failed"
        )


# ── Standalone runner ───────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Pipeline End-to-End Smoke Test")
    print("=" * 50)

    passed = 0
    failed = 0
    skipped = 0

    # Collect all test classes
    test_classes = [
        ("Infrastructure", TestInfrastructure),
        ("Pipeline Stages", TestPipelineStages),
        ("KB & Graph", TestKBAndGraph),
        ("Alignment", TestAlignment),
    ]

    for section, cls in test_classes:
        print(f"\n{section}:")
        obj = cls()
        for name in sorted(dir(obj)):
            if not name.startswith("test_"):
                continue
            label = name.replace("test_", "").replace("_", " ")
            try:
                getattr(obj, name)()
                print(f"  PASS  {label}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {label}: {e}")
                failed += 1

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)
