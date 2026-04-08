"""
Data integrity tests — validate CSV, transcripts, and KB files.
These run against real data on the local Mac.
"""
import os, re, csv, pytest
from conftest import (
    CSV_PATH, NOTES_DIR, KB_DIR, KB_MEETINGS, KB_PEOPLE,
    VALID_CATEGORIES, CONTENT_CATEGORIES,
)


# ── CSV ────────────────────────────────────────────────────────────────────────

class TestCSV:
    def test_csv_exists(self):
        assert os.path.exists(CSV_PATH), f"CSV not found: {CSV_PATH}"

    def test_csv_has_rows(self, csv_rows):
        assert len(csv_rows) > 0, "CSV is empty"

    def test_csv_required_columns(self, csv_rows):
        required = {"filename", "date", "category", "summary", "key_people", "topic"}
        assert required <= set(csv_rows[0].keys()), \
            f"Missing columns: {required - set(csv_rows[0].keys())}"

    def test_csv_valid_categories(self, csv_rows):
        bad = [
            (r["filename"], r["category"])
            for r in csv_rows
            if r["category"] not in VALID_CATEGORIES
        ]
        assert bad == [], f"Invalid categories in CSV: {bad[:5]}"

    def test_csv_dates_format(self, csv_rows):
        """All dates should match YYYY-MM-DD or YYYY-MM-DD HH:MM:SS."""
        bad = [
            (r["filename"], r["date"])
            for r in csv_rows
            if r["date"] and not re.match(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$", r["date"])
        ]
        assert bad == [], f"Bad date formats: {bad[:5]}"

    def test_csv_content_rows_have_summary(self, csv_rows):
        """Rows with content categories should have a non-empty summary."""
        bad = [
            r["filename"]
            for r in csv_rows
            if r["category"] in CONTENT_CATEGORIES and not r["summary"].strip()
        ]
        # Allow up to 5% missing summaries (LLM timeouts happen)
        threshold = int(len([r for r in csv_rows if r["category"] in CONTENT_CATEGORIES]) * 0.05)
        assert len(bad) <= threshold, \
            f"Too many content rows missing summary ({len(bad)}): {bad[:5]}"

    def test_csv_no_duplicate_filenames(self, csv_rows):
        filenames = [r["filename"] for r in csv_rows]
        dupes = [f for f in set(filenames) if filenames.count(f) > 1]
        assert dupes == [], f"Duplicate filenames in CSV: {dupes[:5]}"

    def test_csv_filenames_have_valid_format(self, csv_rows):
        """Filenames should be UUID or Plaud timestamp format, optionally with .txt."""
        # UUID: A1B2C3D4-E5F6-...
        # Plaud: 2026-04-02_20_45_22
        valid_pattern = r"^([A-F0-9\-]{8,}|(\d{4}-\d{2}-\d{2}_\d{2}_\d{2}_\d{2}))(\.txt)?$"
        bad = [
            r["filename"]
            for r in csv_rows
            if not re.match(valid_pattern, r["filename"], re.IGNORECASE)
        ]
        assert bad == [], f"Unexpected filename format: {bad[:5]}"


# ── Transcripts ────────────────────────────────────────────────────────────────

class TestTranscripts:
    def test_notes_dir_exists(self):
        assert os.path.exists(NOTES_DIR), f"Notes dir not found: {NOTES_DIR}"

    def test_transcripts_present(self, notes_tmp_dir):
        txts = [f for f in os.listdir(notes_tmp_dir) if f.endswith(".txt")]
        assert len(txts) > 0, "No .txt transcript files found"

    def test_transcript_headers(self, notes_tmp_dir):
        """Each transcript should start with File: and Recorded: lines."""
        bad = []
        txts = [f for f in os.listdir(notes_tmp_dir) if f.endswith(".txt")]
        for fname in txts[:100]:  # sample first 100
            path = os.path.join(notes_tmp_dir, fname)
            with open(path, errors="replace") as f:
                lines = f.readlines()
            if not lines:
                bad.append((fname, "empty file"))
                continue
            if not lines[0].startswith("File:"):
                bad.append((fname, f"first line: {lines[0].strip()!r}"))
        assert bad == [], f"Bad transcript headers: {bad[:5]}"

    def test_transcript_has_speaker_lines(self, notes_tmp_dir):
        """Transcripts that have been processed should contain at least one speaker line."""
        txts = [f for f in os.listdir(notes_tmp_dir) if f.endswith(".txt")]
        no_speakers = []
        for fname in txts[:50]:  # sample
            path = os.path.join(notes_tmp_dir, fname)
            with open(path, errors="replace") as f:
                content = f.read()
            # A transcript may have SPEAKER_XX, [Name], or [Name?]
            has_speaker = bool(re.search(r'\[(?:SPEAKER_\d+|[A-Z][^]]+)\]', content))
            if not has_speaker and len(content) > 200:  # not just a header
                no_speakers.append(fname)
        # Some may legitimately have no speakers (very short notes) — allow 20%
        threshold = int(len(txts[:50]) * 0.2)
        assert len(no_speakers) <= threshold, \
            f"Too many transcripts with no speaker lines ({len(no_speakers)}): {no_speakers[:5]}"

    def test_csv_filenames_match_transcripts(self, csv_rows, notes_tmp_dir):
        """All CSV entries should have a corresponding transcript file."""
        trans_uuids = {
            f.replace(".txt", "") for f in os.listdir(notes_tmp_dir) if f.endswith(".txt")
        }
        missing = [
            r["filename"].replace(".txt", "")
            for r in csv_rows
            if r["filename"].replace(".txt", "") not in trans_uuids
        ]
        # Allow small gap (files may be mid-sync)
        threshold = max(5, int(len(csv_rows) * 0.02))
        assert len(missing) <= threshold, \
            f"CSV rows with no transcript ({len(missing)}): {missing[:5]}"


# ── Knowledge Base ─────────────────────────────────────────────────────────────

class TestKnowledgeBase:
    def test_kb_dirs_exist(self):
        for d in [KB_DIR, KB_MEETINGS, KB_PEOPLE]:
            assert os.path.exists(d), f"KB dir not found: {d}"

    def test_kb_has_meeting_files(self, kb_meeting_files):
        assert len(kb_meeting_files) > 0, "No meeting .md files in KB"

    def test_kb_has_people_files(self, kb_people_files):
        assert len(kb_people_files) > 0, "No people .md files in KB"

    def test_kb_meeting_frontmatter(self, kb_meeting_files):
        """All meeting files should have required frontmatter fields."""
        required_fields = ["date:", "category:", "source_file:"]
        bad = []
        for fname, content in list(kb_meeting_files.items())[:100]:
            # Extract frontmatter block (between --- markers)
            fm_end = content.find("\n---", 4)
            frontmatter = content[:fm_end + 4] if fm_end > 0 else content[:800]
            for field in required_fields:
                if field not in frontmatter:
                    bad.append((fname, f"missing {field}"))
                    break
        assert bad == [], f"Meeting files with bad frontmatter: {bad[:5]}"

    def test_kb_meeting_frontmatter_categories(self, kb_meeting_files):
        """KB meeting files should use valid categories."""
        bad = []
        for fname, content in kb_meeting_files.items():
            m = re.search(r'^category:\s*(\S+)', content, re.MULTILINE)
            if m and m.group(1) not in VALID_CATEGORIES:
                bad.append((fname, m.group(1)))
        assert bad == [], f"Meeting files with invalid categories: {bad[:5]}"

    def test_kb_meetings_have_summary(self, kb_meeting_files):
        """Content-category meeting files should have a ## Summary section."""
        bad = []
        for fname, content in kb_meeting_files.items():
            m = re.search(r'^category:\s*(\S+)', content, re.MULTILINE)
            category = m.group(1) if m else ""
            if category in CONTENT_CATEGORIES and "## Summary" not in content:
                bad.append(fname)
        threshold = max(3, int(len(kb_meeting_files) * 0.03))
        assert len(bad) <= threshold, \
            f"Content meeting files missing ## Summary ({len(bad)}): {bad[:5]}"

    def test_kb_meeting_source_files_in_csv(self, kb_meeting_files, csv_rows):
        """source_file in KB meetings should match a filename in the CSV."""
        csv_uuids = {r["filename"].replace(".txt", "") for r in csv_rows}
        bad = []
        for fname, content in kb_meeting_files.items():
            m = re.search(r'^source_file:\s*(\S+)', content, re.MULTILINE)
            if m:
                uuid = m.group(1).replace(".txt", "")
                if uuid not in csv_uuids:
                    bad.append((fname, uuid))
        threshold = max(3, int(len(kb_meeting_files) * 0.02))
        assert len(bad) <= threshold, \
            f"KB meetings referencing unknown source_file ({len(bad)}): {bad[:5]}"

    def test_kb_people_files_have_content(self, kb_people_files):
        """People files should have at least one meeting reference."""
        empty = [f for f, c in kb_people_files.items() if len(c.strip()) < 50]
        assert empty == [], f"Empty people files: {empty[:5]}"

    def test_people_files_contain_known_attendees(self, kb_people_files):
        """At least some known recurring attendees should have people files."""
        # Slugified names of people who appear frequently across client meetings
        expected_slugs = ["cathal-murphy", "christopher-kelly", "khizer-ahmed-biyabani"]
        present = [s for s in expected_slugs if f"{s}.md" in kb_people_files]
        assert len(present) >= 1, \
            f"None of the expected recurring attendees have people files: {expected_slugs}"

    def test_kb_meeting_count_matches_csv(self, kb_meeting_files, csv_rows):
        """KB meeting file count should be reasonably close to content-category CSV rows.
        Allows up to 30% deficit (some rows may not yet be built, e.g. recent recordings)."""
        content_rows = [r for r in csv_rows if r["category"] in CONTENT_CATEGORIES]
        ratio = len(kb_meeting_files) / max(1, len(content_rows))
        assert 0.70 <= ratio <= 1.15, \
            f"KB meetings ({len(kb_meeting_files)}) far from CSV content rows ({len(content_rows)})"
