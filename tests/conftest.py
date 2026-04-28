"""
Shared fixtures and constants for pipeline tests.
"""
import os, csv, re, subprocess, pytest

# ── Paths ─────────────────────────────────────────────────────────────────────
CSV_PATH  = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/My Notes Analysis/classification.csv"
)
NOTES_DIR = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/My Notes"
)
KB_DIR        = os.path.expanduser("~/knowledge_base")
KB_MEETINGS   = os.path.join(KB_DIR, "meetings")
KB_PEOPLE     = os.path.join(KB_DIR, "people")
PIPELINE_DIR  = os.path.dirname(os.path.dirname(__file__))

UBUNTU_HOST      = "eoin@nvidiaubuntubox"
UBUNTU_TRANS_DIR = "/home/eoin/audio-inbox/Transcriptions"
UBUNTU_CSV       = "/home/eoin/audio-inbox/classification.csv"

VALID_CATEGORIES = {
    "NTA", "DCC", "DFB", "Diotima", "Paradigm", "ADAPT", "TBS", "LCC",
    "other:blank", "other:personal", "other:conference", "other:lgma"
}

CONTENT_CATEGORIES = {"NTA", "DCC", "Diotima", "Paradigm", "ADAPT", "TBS", "LCC"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def ubuntu_reachable():
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
             UBUNTU_HOST, "echo ok"],
            capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def rsync_to_tmp(src_dir, tmp_dir):
    """Rsync .txt files from iCloud directory to /tmp to avoid EDEADLK."""
    os.makedirs(tmp_dir, exist_ok=True)
    subprocess.run(
        ["rsync", "-a", "--ignore-errors", "--include=*.txt", "--exclude=*",
         src_dir + "/", tmp_dir + "/"],
        capture_output=True, timeout=300
    )
    return tmp_dir


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def csv_rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="session")
def kb_meeting_files():
    """Returns {filename: content} for all KB meeting markdown files."""
    result = {}
    for fname in os.listdir(KB_MEETINGS):
        if fname.endswith(".md"):
            with open(os.path.join(KB_MEETINGS, fname), errors="replace") as f:
                result[fname] = f.read()
    return result


@pytest.fixture(scope="session")
def kb_people_files():
    """Returns {filename: content} for all KB people markdown files."""
    result = {}
    for fname in os.listdir(KB_PEOPLE):
        if fname.endswith(".md"):
            with open(os.path.join(KB_PEOPLE, fname), errors="replace") as f:
                result[fname] = f.read()
    return result


@pytest.fixture(scope="session")
def notes_tmp_dir(tmp_path_factory):
    """Notes directory rsynced to /tmp to avoid iCloud EDEADLK."""
    tmp = str(tmp_path_factory.mktemp("notes"))
    return rsync_to_tmp(NOTES_DIR, tmp)


# ── Markers ───────────────────────────────────────────────────────────────────
def pytest_configure(config):
    config.addinivalue_line("markers", "slow: marks tests as slow (LLM/GPU calls)")
    config.addinivalue_line("markers", "ubuntu: marks tests that require Ubuntu SSH")


def pytest_collection_modifyitems(config, items):
    skip_slow   = pytest.mark.skip(reason="pass --run-slow to run")
    skip_ubuntu = pytest.mark.skip(reason="Ubuntu not reachable")
    for item in items:
        if "slow" in item.keywords and not config.getoption("--run-slow", default=False):
            item.add_marker(skip_slow)
        if "ubuntu" in item.keywords and not ubuntu_reachable():
            item.add_marker(skip_ubuntu)


def pytest_addoption(parser):
    parser.addoption("--run-slow", action="store_true", default=False,
                     help="Run slow integration tests (LLM/GPU)")
