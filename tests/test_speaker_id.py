"""
Unit tests for speaker identification logic.
Tests pure functions from identify_speakers.py using real data.
"""
import os, sys, re, json, pytest
import numpy as np

# Add pipeline root and ubuntu/ to path so we can import the scripts
_repo_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _repo_root)
sys.path.insert(0, os.path.join(_repo_root, "ubuntu"))

from identify_speakers import (
    cosine_sim,
    expand_names,
    extract_name_cues,
    voice_match,
    CATEGORY_NAME_EXPANSIONS,
    VOICE_THRESHOLD_HIGH,
    VOICE_THRESHOLD_MEDIUM,
)
from conftest import KB_MEETINGS, CSV_PATH


# ── cosine_sim ─────────────────────────────────────────────────────────────────

class TestCosineSim:
    def test_identical_vectors(self):
        v = [1.0, 0.5, -0.3, 0.8]
        assert cosine_sim(v, v) == pytest.approx(1.0, abs=1e-5)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_sim(a, b) == pytest.approx(0.0, abs=1e-5)

    def test_opposite_vectors(self):
        v = [1.0, 0.5, -0.3]
        assert cosine_sim(v, [-x for x in v]) == pytest.approx(-1.0, abs=1e-5)

    def test_zero_vector_no_crash(self):
        """Zero vector should not raise ZeroDivisionError."""
        result = cosine_sim([0.0, 0.0, 0.0], [1.0, 0.0, 0.0])
        assert result == pytest.approx(0.0, abs=1e-4)

    def test_192_dim_random(self):
        """Should work with 192-dim ECAPA vectors."""
        rng = np.random.default_rng(42)
        a = rng.standard_normal(192).tolist()
        b = rng.standard_normal(192).tolist()
        result = cosine_sim(a, b)
        assert -1.0 <= result <= 1.0

    def test_similar_vectors_high_score(self):
        """Slightly perturbed vector should have high similarity."""
        rng = np.random.default_rng(7)
        base = rng.standard_normal(192)
        perturbed = base + rng.standard_normal(192) * 0.05
        assert cosine_sim(base.tolist(), perturbed.tolist()) > 0.95


# ── expand_names ───────────────────────────────────────────────────────────────

class TestExpandNames:
    def test_dcc_kizzer(self):
        result = expand_names("kizzer", "DCC")
        assert result == "Khizer Ahmed Biyabani"

    def test_dcc_chris(self):
        result = expand_names("chris", "DCC")
        assert result == "Christopher Kelly"

    def test_nta_cathal(self):
        result = expand_names("cathal", "NTA")
        assert result == "Cathal Bellew"

    def test_diotima_masa(self):
        result = expand_names("masa", "Diotima")
        assert result == "Mahsa Mahdinejad"

    def test_unknown_category_passthrough(self):
        result = expand_names("john doe", "TBS")
        assert result == "john doe"

    def test_multiple_names_in_csv_string(self):
        result = expand_names("kizzer, richie, stephen", "DCC")
        assert "Khizer Ahmed Biyabani" in result
        assert "Richie Shakespeare" in result
        assert "Stephen Rigney" in result

    def test_unknown_name_passthrough(self):
        result = expand_names("margaret", "DCC")
        assert result == "margaret"

    def test_adapt_kizzer_same_as_dcc(self):
        assert expand_names("kizzer", "ADAPT") == expand_names("kizzer", "DCC")

    def test_all_categories_have_entries(self):
        """Each category in CATEGORY_NAME_EXPANSIONS should have at least one entry."""
        for cat, entries in CATEGORY_NAME_EXPANSIONS.items():
            assert len(entries) > 0, f"Category {cat} has no expansions"

    def test_no_cross_category_bleed(self):
        """siobhan should resolve differently in Diotima vs NTA."""
        diotima = expand_names("siobhan", "Diotima")
        nta = expand_names("siobhan", "NTA")
        assert diotima != nta
        assert "Ryan" in diotima
        assert "Quinn" in nta


# ── extract_name_cues ──────────────────────────────────────────────────────────

class TestExtractNameCues:
    def _make_transcript(self, lines):
        return "\n".join(lines)

    def test_speaker_addresses_two_people_is_not_them(self):
        content = self._make_transcript([
            "[SPEAKER_00] 00:01 - Hey Eoin, how are you doing?",
            "[SPEAKER_00] 00:10 - Cathal, what do you think about this?",
        ])
        cues = extract_name_cues(content, ["Eoin Lane", "Cathal Bellew", "Declan Sheehan"], "NTA")
        assert any("HARD CONSTRAINT" in c and "SPEAKER_00" in c for c in cues), \
            f"Expected HARD CONSTRAINT for SPEAKER_00 addressing 2+ people, got: {cues}"

    def test_speaker_addresses_one_person_is_hint(self):
        content = self._make_transcript([
            "[SPEAKER_01] 00:01 - Thanks Eoin, that makes sense.",
        ])
        cues = extract_name_cues(content, ["Eoin Lane", "Cathal Bellew"], "NTA")
        assert any("HINT" in c and "SPEAKER_01" in c for c in cues), \
            f"Expected HINT for SPEAKER_01 mentioning one name, got: {cues}"

    def test_no_cues_for_unknown_names(self):
        content = self._make_transcript([
            "[SPEAKER_00] 00:01 - The project looks good.",
            "[SPEAKER_01] 00:10 - Yes I agree completely.",
        ])
        cues = extract_name_cues(content, ["Eoin Lane", "Cathal Bellew"], "NTA")
        assert cues == []

    def test_dcc_nickname_expansion_in_cues(self):
        """'kizzer' in transcript should be resolved to full name in cue."""
        content = self._make_transcript([
            "[SPEAKER_00] 00:01 - Kizzer, can you take the lead on this?",
            "[SPEAKER_00] 00:15 - And Chris, you'll support?",
        ])
        cues = extract_name_cues(content, ["Eoin Lane", "Khizer Ahmed Biyabani", "Christopher Kelly"], "DCC")
        hard_constraints = [c for c in cues if "HARD CONSTRAINT" in c and "SPEAKER_00" in c]
        assert hard_constraints, f"Expected HARD CONSTRAINT, got: {cues}"
        assert "Khizer Ahmed Biyabani" in hard_constraints[0]
        assert "Christopher Kelly" in hard_constraints[0]

    def test_owen_lane_resolves_to_eoin(self):
        """'Owen' in transcript should resolve to Eoin Lane."""
        content = self._make_transcript([
            "[SPEAKER_01] 00:01 - Owen, what's your view on that?",
        ])
        cues = extract_name_cues(content, ["Eoin Lane", "Cathal Bellew"], "NTA")
        assert any("Eoin Lane" in c for c in cues)


# ── voice_match ────────────────────────────────────────────────────────────────

class TestVoiceMatch:
    def _make_embedding(self, seed, dim=192):
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(dim)
        return (v / np.linalg.norm(v)).tolist()

    def _make_catalog(self, people):
        """people: {name: [seed1, seed2, ...]}"""
        return {
            name: {"embeddings": [self._make_embedding(s) for s in seeds]}
            for name, seeds in people.items()
        }

    def _make_embedding_file(self, tmp_path, uuid, speaker_embeddings):
        """speaker_embeddings: {label: seed}"""
        data = {
            label: {"embedding": self._make_embedding(seed), "n_segments": 3}
            for label, seed in speaker_embeddings.items()
        }
        p = tmp_path / f"{uuid}.json"
        p.write_text(json.dumps(data))
        return str(tmp_path)

    def test_exact_match_returns_high_confidence(self, tmp_path, monkeypatch):
        """Embedding identical to catalog should match at high confidence."""
        emb = self._make_embedding(42)
        catalog = {"Eoin Lane": {"embeddings": [emb]}}
        uuid = "TEST-UUID-001"
        data = {"SPEAKER_00": {"embedding": emb, "n_segments": 5}}
        (tmp_path / f"{uuid}.json").write_text(json.dumps(data))

        import identify_speakers
        monkeypatch.setattr(identify_speakers, "EMBEDDINGS_DIR", str(tmp_path))

        result = voice_match(uuid, ["SPEAKER_00"], catalog)
        assert "SPEAKER_00" in result
        assert result["SPEAKER_00"]["confidence"] == "high"
        assert result["SPEAKER_00"]["name"] == "Eoin Lane"

    def test_orthogonal_embedding_no_match(self, tmp_path, monkeypatch):
        """Orthogonal embedding should not match anyone."""
        catalog_emb = [1.0] + [0.0] * 191
        catalog = {"Eoin Lane": {"embeddings": [catalog_emb]}}
        query_emb = [0.0, 1.0] + [0.0] * 190

        uuid = "TEST-UUID-002"
        data = {"SPEAKER_00": {"embedding": query_emb, "n_segments": 3}}
        (tmp_path / f"{uuid}.json").write_text(json.dumps(data))

        import identify_speakers
        monkeypatch.setattr(identify_speakers, "EMBEDDINGS_DIR", str(tmp_path))

        result = voice_match(uuid, ["SPEAKER_00"], catalog)
        assert "SPEAKER_00" not in result

    def test_medium_confidence_threshold(self, tmp_path, monkeypatch):
        """Embedding with similarity between 0.70 and 0.80 → medium confidence."""
        rng = np.random.default_rng(99)
        base = rng.standard_normal(192)
        base /= np.linalg.norm(base)
        # Perturb enough to land ~0.75 similarity
        noise = rng.standard_normal(192)
        noise -= noise.dot(base) * base  # orthogonal component
        perturbed = base + noise * 0.9
        perturbed /= np.linalg.norm(perturbed)

        catalog = {"Test Person": {"embeddings": [base.tolist()]}}
        uuid = "TEST-UUID-003"
        data = {"SPEAKER_00": {"embedding": perturbed.tolist(), "n_segments": 3}}
        (tmp_path / f"{uuid}.json").write_text(json.dumps(data))

        import identify_speakers
        monkeypatch.setattr(identify_speakers, "EMBEDDINGS_DIR", str(tmp_path))

        result = voice_match(uuid, ["SPEAKER_00"], catalog)
        sim = cosine_sim(perturbed.tolist(), base.tolist())
        if sim >= VOICE_THRESHOLD_HIGH:
            assert result.get("SPEAKER_00", {}).get("confidence") == "high"
        elif sim >= VOICE_THRESHOLD_MEDIUM:
            assert result.get("SPEAKER_00", {}).get("confidence") == "medium"
        else:
            assert "SPEAKER_00" not in result

    def test_no_embedding_file_returns_empty(self, tmp_path, monkeypatch):
        catalog = {"Eoin Lane": {"embeddings": [self._make_embedding(1)]}}
        import identify_speakers
        monkeypatch.setattr(identify_speakers, "EMBEDDINGS_DIR", str(tmp_path))
        result = voice_match("NONEXISTENT-UUID", ["SPEAKER_00"], catalog)
        assert result == {}

    def test_empty_catalog_returns_empty(self, tmp_path, monkeypatch):
        emb = self._make_embedding(42)
        uuid = "TEST-UUID-004"
        data = {"SPEAKER_00": {"embedding": emb, "n_segments": 3}}
        (tmp_path / f"{uuid}.json").write_text(json.dumps(data))
        import identify_speakers
        monkeypatch.setattr(identify_speakers, "EMBEDDINGS_DIR", str(tmp_path))
        result = voice_match(uuid, ["SPEAKER_00"], {})
        assert result == {}

    def test_best_match_selected_from_multiple_people(self, tmp_path, monkeypatch):
        """Should pick the closest person in the catalog."""
        base = self._make_embedding(10)
        close = [x + 0.01 for x in base]  # very close
        close_arr = np.array(close, dtype=np.float32)
        close_arr /= np.linalg.norm(close_arr)

        far = self._make_embedding(999)  # unrelated

        catalog = {
            "Eoin Lane": {"embeddings": [close_arr.tolist()]},
            "Cathal Bellew": {"embeddings": [far]},
        }
        uuid = "TEST-UUID-005"
        data = {"SPEAKER_00": {"embedding": base, "n_segments": 5}}
        (tmp_path / f"{uuid}.json").write_text(json.dumps(data))

        import identify_speakers
        monkeypatch.setattr(identify_speakers, "EMBEDDINGS_DIR", str(tmp_path))

        result = voice_match(uuid, ["SPEAKER_00"], catalog)
        if "SPEAKER_00" in result:
            assert result["SPEAKER_00"]["name"] == "Eoin Lane"


# ── Real-data smoke tests ──────────────────────────────────────────────────────

class TestRealData:
    def test_csv_can_be_loaded_for_speaker_id(self, csv_rows):
        """Verify we can extract key_people from CSV for at least one DCC row."""
        dcc_rows = [r for r in csv_rows if r["category"] == "DCC" and r.get("key_people")]
        assert dcc_rows, "No DCC rows with key_people in CSV"
        sample = dcc_rows[0]
        expanded = expand_names(sample["key_people"], "DCC")
        # Should not crash and should return a string
        assert isinstance(expanded, str)
        assert len(expanded) > 0

    def test_kb_meetings_have_source_file_field(self, kb_meeting_files):
        """At least 90% of KB meeting files should have source_file in frontmatter."""
        total = len(kb_meeting_files)
        with_source = sum(
            1 for c in kb_meeting_files.values()
            if re.search(r'^source_file:', c, re.MULTILINE)
        )
        assert with_source / max(1, total) >= 0.90, \
            f"Only {with_source}/{total} KB meetings have source_file"

    def test_attendees_section_present_in_some_meetings(self, kb_meeting_files):
        """At least some KB meetings should have the **Attendees:** section."""
        with_attendees = sum(
            1 for c in kb_meeting_files.values()
            if "**Attendees:**" in c
        )
        assert with_attendees > 0, "No KB meetings have **Attendees:** section"
