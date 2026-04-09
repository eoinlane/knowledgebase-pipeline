"""
Shared configuration for the knowledgebase pipeline.
Imported by both Ubuntu and Mac scripts.
"""

# ── Ollama (ollama-box VM) ──────────────────────────────────────────────────
OLLAMA_URL = "http://192.168.0.70:11434/api/chat"
MODEL = "qwen2.5:14b"

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
}

# Categories that shouldn't be overridden by speaker-based reclassification
KEEP_CATEGORIES = {"other:personal", "other:conference", "other:lgma", "other:blank"}
