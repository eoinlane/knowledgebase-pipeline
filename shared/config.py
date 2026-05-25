"""
Shared configuration for the knowledgebase pipeline.
Imported by both Ubuntu and Mac scripts.
"""

# ── Ollama (ollama-box VM) ──────────────────────────────────────────────────
OLLAMA_URL = "http://192.168.0.70:11434/api/chat"
MODEL = "qwen2.5:14b"
# Pinned digest as of 2026-05-24. Confirmed identical April→May benchmark
# predictions, so the underlying weights haven't shifted. If `ollama pull`
# ever changes the digest, the model has been updated underneath us and we
# need to re-benchmark. Run: ssh ollama-box "ollama list | grep qwen2.5:14b"
OLLAMA_MODEL_DIGEST_EXPECTED = "sha256:7cdf5a0187d5c58cc5d369b255592f7841d1c4696d45a8c8a9489440385b22f6"

# ── Anthropic via LiteLLM (Ubuntu port 4000) ────────────────────────────────
# These are the model NAMES recognised by the LiteLLM proxy — the version
# pinning happens in /home/eoin/litellm-config.yaml on Ubuntu, which maps
# the short alias `claude-haiku-4-5` → `anthropic/claude-haiku-4-5-20251001`.
# If we ever call Anthropic directly (bypassing LiteLLM), use the long-form
# name; through LiteLLM, use the short alias the YAML registers.
HAIKU_MODEL = "claude-haiku-4-5"
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL_PINNED = "claude-haiku-4-5-20251001"  # underlying Anthropic version
LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_URL_REMOTE = "http://100.121.184.27:4000/v1/chat/completions"

# ── Infrastructure ──────────────────────────────────────────────────────────
UBUNTU_HOST = "eoin@nvidiaubuntubox"
MAC_HOST = "eoin@100.103.128.44"
OLLAMA_BOX = "http://192.168.0.70:11434"

# ── Person → primary category mapping ───────────────────────────────────────
# Source of truth: if this person is in a meeting, it's this category.
# Used by reclassify_by_speaker.py and can be used by classify_transcript.py.
PERSON_CATEGORY = {
    # NTA
    "Cathal Bellew": "NTA",
    "Declan Sheehan": "NTA",
    "Alex McKenzie": "NTA",
    "Philip L'Estrange": "NTA",
    "Ger Regan": "NTA",
    "Neil Sutch": "NTA",
    "Audrey O'Hara": "NTA",
    "Gary White": "NTA",
    "Dominic Hannigan": "NTA",
    "Mark McDermott": "NTA",

    # DCC
    "Re Dubhthaigh": "DCC",
    "Ray Duffy": "DCC",  # Re Dubhthaigh's English name
    "Christopher Kelly": "DCC",
    "Richie Shakespeare": "DCC",
    "Shunyu Ji": "DCC",
    "Tom Curran": "DCC",
    "Jamie Cudden": "DCC",
    "Stephen Rigney": "DCC",
    "Todd Asher": "DCC",  # Bloomberg Associates, AI Lab Smart Cities partner
    "Serena McIntosh": "DCC",  # Bloomberg Associates, AI Lab Smart Cities partner
    "Allan McDonald": "DCC",  # DCC digital lead

    # ADAPT
    "Declan McKibben": "ADAPT",
    "Khizer Ahmed Biyabani": "ADAPT",

    # DFB
    "Rob Howell": "DFB",

    # Diotima
    "Siobhan Ryan": "Diotima",
    "Jonathan Dempsey": "Diotima",
    "Mahsa Mahdinejad": "Diotima",
    "Tom Pollock": "Diotima",

    # Paradigm
    "Guy Rackham": "Paradigm",
    "Arijit Sircar": "Paradigm",
    "Sarah Broderick": "Paradigm",
    "Eddy Moretti": "Paradigm",

    # TBS
    "Kisito Futonge Nzembayie": "TBS",
    "Daniel Coughlan": "TBS",
    "Neil Dunne": "TBS",
    "Sinead Monaghan": "TBS",

    # LCC (Limerick County Council) — local-government client, parallel to DCC.
    # Tag with "lgma" in meeting frontmatter for the broader umbrella.
    "Alan Dooley": "LCC",
}

# Categories that shouldn't be overridden by speaker-based reclassification
KEEP_CATEGORIES = {"other:personal", "other:conference", "other:lgma", "other:blank", "FutureBusiness"}

# ── WhisperX initial_prompt ─────────────────────────────────────────────────
# Biases the decoder toward known names and domain vocabulary. Targets the
# recurring mishearing class: "Owen Lane" (=Eoin), "Cathal Murphy" (=Bellew),
# "Aoife Mangan" (=Mannion), "Altera" (=Oterra), "Valley 1 library"
# (=Ballymun library), "Grant" (=Grand/Chris/Neil), etc.
# Whisper allows ~224 tokens for initial_prompt; keep this under that budget.
WHISPER_INITIAL_PROMPT = (
    "Meeting with Eoin Lane. "
    "NTA: Cathal Bellew, Declan Sheehan, Alex McKenzie, Gary White, Ger Regan, "
    "Audrey O'Hara, Aoife Mannion, Tomás Kelly, Elizabeth Heggs. "
    "DCC: Pat Nestor, Jamie Cudden, Richie Shakespeare, Christopher Kelly, "
    "Allan Macdonald, Stephen Rigney, Tom Curran, Shunyu Ji, "
    "Khizer Ahmed Biyabani, Rob Howell, Mary O'Brien. "
    "ADAPT: Declan McKibben, Ashish Kumar Jha, Paul Pierotti, Aarthi Kumar. "
    "Diotima: Siobhan Ryan, Jonathan Dempsey, Mahsa Mahdinejad. "
    "Paradigm: Guy Rackham, Arijit Sircar. "
    "TBS: Neil Dunne, Kisito Nzembayie. "
    "LCC: Alan Dooley. "
    "Terms: BCMS, DAC, Part M, OVN, PQ, AGB, WACI, BIM, LiDAR, Bentley CDE, "
    "Building Control, Disability Access Certificate, Sogeti, Oterra, Ballymun Library."
)
