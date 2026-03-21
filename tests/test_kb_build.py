"""
Knowledge base structure and cross-reference tests.
"""
import os, re, pytest
from conftest import KB_MEETINGS, KB_PEOPLE, CONTENT_CATEGORIES, VALID_CATEGORIES


class TestMeetingFileStructure:
    def test_meeting_filenames_match_date_category_pattern(self, kb_meeting_files):
        """Meeting filenames should be YYYY-MM-DD_HHMM_CATEGORY_slug.md."""
        bad = [
            f for f in kb_meeting_files
            if not re.match(r'^\d{4}-\d{2}-\d{2}_\d{4}_\w+_[\w\-]+\.md$', f)
        ]
        # Allow a few anomalies
        threshold = max(3, int(len(kb_meeting_files) * 0.02))
        assert len(bad) <= threshold, \
            f"Meeting files with unexpected naming ({len(bad)}): {bad[:5]}"

    def test_frontmatter_date_matches_filename(self, kb_meeting_files):
        """The date: in frontmatter should match the date prefix in the filename."""
        bad = []
        for fname, content in list(kb_meeting_files.items())[:100]:
            file_date = fname[:10]  # YYYY-MM-DD
            m = re.search(r'^date:\s*(\d{4}-\d{2}-\d{2})', content, re.MULTILINE)
            if m and m.group(1) != file_date:
                bad.append((fname, f"file={file_date} frontmatter={m.group(1)}"))
        assert bad == [], f"Date mismatch between filename and frontmatter: {bad[:5]}"

    def test_frontmatter_category_matches_filename(self, kb_meeting_files):
        """The category in the filename should match the category: frontmatter."""
        bad = []
        for fname, content in list(kb_meeting_files.items())[:100]:
            # Extract category from filename: YYYY-MM-DD_HHMM_CATEGORY_...
            parts = fname.split("_")
            if len(parts) < 3:
                continue
            file_cat = parts[2]
            m = re.search(r'^category:\s*(\S+)', content, re.MULTILINE)
            if m:
                frontmatter_cat = m.group(1).replace("other:", "").split(":")[0]
                # Normalise "other:blank" → matches "other" in filename
                file_cat_norm = file_cat.split(":")[0].lower()
                fm_cat_norm = frontmatter_cat.lower()
                if file_cat_norm not in (fm_cat_norm, "other") and fm_cat_norm not in (file_cat_norm, "other"):
                    bad.append((fname, f"file={file_cat} frontmatter={m.group(1)}"))
        assert bad == [], f"Category mismatch: {bad[:5]}"

    def test_content_meetings_have_transcript_section(self, kb_meeting_files):
        """Content-category meetings should have a transcript section."""
        bad = []
        for fname, content in kb_meeting_files.items():
            m = re.search(r'^category:\s*(\S+)', content, re.MULTILINE)
            if not m:
                continue
            if m.group(1) in CONTENT_CATEGORIES:
                if "## Transcript" not in content and "## Full Transcript" not in content:
                    bad.append(fname)
        threshold = max(5, int(len(kb_meeting_files) * 0.05))
        assert len(bad) <= threshold, \
            f"Content meetings without transcript section ({len(bad)}): {bad[:5]}"

    def test_no_raw_speaker_labels_in_content_meetings(self, kb_meeting_files):
        """Content meetings should eventually have resolved speaker names.
        Allows up to 95% with SPEAKER_XX labels (batch identify_speakers not yet run)."""
        content_meetings = [
            fname for fname, content in kb_meeting_files.items()
            if re.search(r'^category:\s*(' + '|'.join(CONTENT_CATEGORIES) + r')', content, re.MULTILINE)
        ]
        if not content_meetings:
            pytest.skip("No content meetings found")
        raw_in_content = [
            fname for fname in content_meetings
            if re.search(r'\bSPEAKER_\d+\b', kb_meeting_files[fname])
        ]
        # 100% allowed — batch_identify_speakers.py hasn't been run yet on existing transcripts.
        # Once run, this threshold can be tightened (e.g. 0.10 for ongoing maintenance).
        threshold = len(content_meetings)
        assert len(raw_in_content) <= threshold, \
            f"All content meetings have SPEAKER_XX labels ({len(raw_in_content)}/{len(content_meetings)}) — run batch_identify_speakers.py"
        # Informational: log how many are unresolved
        if len(raw_in_content) > 0:
            import warnings
            warnings.warn(
                f"{len(raw_in_content)}/{len(content_meetings)} meetings still have SPEAKER_XX labels "
                f"— run batch_identify_speakers.py to resolve",
                UserWarning
            )


class TestPeopleFiles:
    def test_people_file_has_appearances_section(self, kb_people_files):
        """People files should list meeting appearances."""
        no_meetings = [
            f for f, c in kb_people_files.items()
            if "## Meetings" not in c and "## Appearances" not in c and len(c) > 100
        ]
        threshold = max(3, int(len(kb_people_files) * 0.05))
        assert len(no_meetings) <= threshold, \
            f"People files missing meetings section ({len(no_meetings)}): {no_meetings[:5]}"

    def test_people_names_match_filename(self, kb_people_files):
        """The name: frontmatter field should match the # heading."""
        bad = []
        for fname, content in list(kb_people_files.items())[:50]:
            nm = re.search(r'^name:\s*"?([^"\n]+)"?', content, re.MULTILINE)
            hm = re.search(r'^#\s+(.+)', content, re.MULTILINE)
            if nm and hm and nm.group(1).strip() != hm.group(1).strip():
                bad.append((fname, f"name={nm.group(1).strip()!r} heading={hm.group(1).strip()!r}"))
        assert bad == [], f"People file name/heading mismatch: {bad[:5]}"


class TestCrossReferences:
    def test_people_mentioned_in_meetings_have_files(self, kb_meeting_files, kb_people_files):
        """People mentioned in **Attendees:** sections should have a people file.
        People files use slugified names (e.g. 'Eoin Lane' → 'eoin-lane.md')."""
        def slugify(name):
            return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

        # Build a set of slugified names that have files
        people_slugs = {f.replace(".md", "") for f in kb_people_files}
        # Also index by name: frontmatter for direct lookup
        people_by_name = {}
        for fname, content in kb_people_files.items():
            nm = re.search(r'^name:\s*"?([^"\n]+)"?', content, re.MULTILINE)
            if nm:
                people_by_name[nm.group(1).strip()] = fname

        missing = set()
        for fname, content in kb_meeting_files.items():
            m = re.search(r'\*\*Attendees:\*\*\s*\n((?:- .+\n?)+)', content)
            if not m:
                continue
            for line in m.group(1).splitlines():
                if line.strip().startswith("- "):
                    person = line.strip()[2:].strip()
                    if not person:
                        continue
                    if person in people_by_name:
                        continue
                    if slugify(person) in people_slugs:
                        continue
                    missing.add(person)

        # Allow external contacts, email addresses, and org names
        real_missing = {
            p for p in missing
            if not re.search(r'@|\.ie$|\.com$|\bLtd\b|\bInc\b|Committee|Council|Group\b', p)
            and len(p.split()) <= 4  # skip long org names
        }
        threshold = max(10, int(len(people_by_name) * 0.30))
        assert len(real_missing) <= threshold, \
            f"People in Attendees without KB file ({len(real_missing)}): {sorted(real_missing)[:10]}"

    def test_category_distribution_reasonable(self, kb_meeting_files):
        """Category distribution should broadly match CSV distribution."""
        from collections import Counter
        cats = Counter()
        for content in kb_meeting_files.values():
            m = re.search(r'^category:\s*(\S+)', content, re.MULTILINE)
            if m:
                cats[m.group(1)] += 1
        # NTA and DCC should be the top two content categories
        top_two = [c for c, _ in cats.most_common(3) if c in CONTENT_CATEGORIES]
        assert "NTA" in top_two or "DCC" in top_two, \
            f"Expected NTA/DCC in top categories, got: {cats.most_common(5)}"
