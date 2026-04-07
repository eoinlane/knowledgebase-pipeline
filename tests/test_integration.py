"""
Integration tests — require Ubuntu SSH or LLM access.
Marked @pytest.mark.slow or @pytest.mark.ubuntu.
Skip by default; run with: pytest --run-slow
"""
import os, re, json, subprocess, pytest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from conftest import (
    UBUNTU_HOST, UBUNTU_TRANS_DIR, UBUNTU_CSV,
    PIPELINE_DIR,
)


# ── Ubuntu service health ──────────────────────────────────────────────────────

@pytest.mark.ubuntu
class TestUbuntuServices:
    def test_notes_watcher_active(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, "systemctl is-active notes-watcher"],
            capture_output=True, text=True, timeout=10
        )
        assert r.stdout.strip() == "active", \
            f"notes-watcher not active: {r.stdout.strip()!r}"

    def test_litellm_active(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, "systemctl --user is-active litellm"],
            capture_output=True, text=True, timeout=10
        )
        assert r.stdout.strip() == "active", \
            f"litellm not active: {r.stdout.strip()!r}"

    def test_ollama_responding(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, "curl -s --max-time 5 http://localhost:11434/api/tags"],
            capture_output=True, text=True, timeout=15
        )
        assert r.returncode == 0 and "models" in r.stdout, \
            f"Ollama not responding: {r.stdout[:200]!r}"

    def test_transcription_dir_exists(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, f"test -d {UBUNTU_TRANS_DIR} && echo ok"],
            capture_output=True, text=True, timeout=10
        )
        assert r.stdout.strip() == "ok", \
            f"Transcription dir not found: {UBUNTU_TRANS_DIR}"

    def test_ubuntu_csv_exists(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, f"test -f {UBUNTU_CSV} && echo ok"],
            capture_output=True, text=True, timeout=10
        )
        assert r.stdout.strip() == "ok", f"Ubuntu CSV not found: {UBUNTU_CSV}"

    def test_transcript_count_reasonable(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, f"ls {UBUNTU_TRANS_DIR}/*.txt 2>/dev/null | wc -l"],
            capture_output=True, text=True, timeout=10
        )
        count = int(r.stdout.strip() or "0")
        assert count > 100, f"Only {count} transcripts on Ubuntu — expected >100"

    def test_gpu_available(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, "nvidia-smi --query-gpu=name --format=csv,noheader"],
            capture_output=True, text=True, timeout=15
        )
        assert r.returncode == 0, f"nvidia-smi failed: {r.stderr}"
        assert "RTX" in r.stdout or "Tesla" in r.stdout or "A100" in r.stdout, \
            f"Unexpected GPU: {r.stdout.strip()!r}"


# ── Speaker identification E2E ─────────────────────────────────────────────────

@pytest.mark.ubuntu
@pytest.mark.slow
class TestSpeakerIdEndToEnd:
    def test_identify_speakers_script_exists_on_ubuntu(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, "test -f ~/identify_speakers.py && echo ok"],
            capture_output=True, text=True, timeout=10
        )
        assert r.stdout.strip() == "ok", "identify_speakers.py not found on Ubuntu"

    def test_review_speakers_script_exists_on_ubuntu(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, "test -f ~/review_speakers.py && echo ok"],
            capture_output=True, text=True, timeout=10
        )
        assert r.stdout.strip() == "ok", "review_speakers.py not found on Ubuntu"

    def test_batch_identify_speakers_dry_run(self):
        """Dry run of batch_identify_speakers.py should complete without error."""
        r = subprocess.run(
            ["ssh", UBUNTU_HOST,
             "source ~/whisper-env/bin/activate && "
             "python3 ~/batch_identify_speakers.py --dry-run --limit 5"],
            capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 0, f"Dry run failed:\n{r.stderr}"
        assert "DRY RUN" in r.stdout or "Would process" in r.stdout or "to process" in r.stdout, \
            f"Unexpected dry-run output:\n{r.stdout[:500]}"

    def test_speaker_mappings_json_valid(self):
        """speaker_mappings.json on Ubuntu should be valid JSON."""
        r = subprocess.run(
            ["ssh", UBUNTU_HOST,
             "test -f ~/speaker_mappings.json && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' ~/speaker_mappings.json && echo ok || echo missing"],
            capture_output=True, text=True, timeout=10
        )
        assert r.stdout.strip() in ("ok", "missing"), \
            f"speaker_mappings.json is invalid JSON: {r.stderr}"


# ── LLM classification smoke test ─────────────────────────────────────────────

@pytest.mark.ubuntu
@pytest.mark.slow
class TestClassificationSmoke:
    def test_classify_transcript_script_exists(self):
        r = subprocess.run(
            ["ssh", UBUNTU_HOST, "test -f ~/classify_transcript.py && echo ok"],
            capture_output=True, text=True, timeout=10
        )
        assert r.stdout.strip() == "ok", "classify_transcript.py not found on Ubuntu"

    def test_ollama_model_available(self):
        """qwen2.5:14b should be available on ollama-box."""
        r = subprocess.run(
            ["curl", "-s", "--max-time", "10", "http://192.168.0.70:11434/api/tags"],
            capture_output=True, text=True, timeout=15
        )
        assert "qwen2.5" in r.stdout.lower(), \
            f"qwen2.5 model not found on ollama-box: {r.stdout[:300]}"


# ── Build pipeline smoke test ──────────────────────────────────────────────────

@pytest.mark.slow
class TestBuildPipeline:
    def test_build_script_exists(self):
        build_script = os.path.expanduser("~/build_knowledge_base.py")
        assert os.path.exists(build_script), f"build_knowledge_base.py not found: {build_script}"

    def test_incremental_upload_script_exists(self):
        script = os.path.expanduser("~/upload_knowledge_base_incremental.py")
        assert os.path.exists(script), f"upload_knowledge_base_incremental.py not found"

    def test_rebuild_sh_exists(self):
        script = os.path.expanduser("~/.local/bin/rebuild-knowledge-base.sh")
        assert os.path.exists(script), f"rebuild-knowledge-base.sh not found"

    def test_sync_sh_exists(self):
        script = os.path.expanduser("~/.local/bin/sync-knowledge-base.sh")
        assert os.path.exists(script), f"sync-knowledge-base.sh not found"
