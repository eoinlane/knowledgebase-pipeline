"""
Category-specific name expansion tables.
Maps WhisperX mishearings and short names to full names.
Used by identify_speakers.py for speaker identification.
"""

CATEGORY_NAME_EXPANSIONS = {
    "DCC": {
        "chris": "Christopher Kelly",
        "christopher": "Christopher Kelly",
        "sean": "Shunyu Ji",
        "shawn": "Shunyu Ji",
        "ray": "Re Dubhthaigh",
        "ray duffy": "Re Dubhthaigh",
        "kizzer": "Khizer Ahmed Biyabani",
        "khizer": "Khizer Ahmed Biyabani",
        "kizer": "Khizer Ahmed Biyabani",
        "kaiser": "Khizer Ahmed Biyabani",
        "richie": "Richie Shakespeare",
        "stephen": "Stephen Rigney",
        "eoin swift": "Eoin Swift",
        "swift": "Eoin Swift",
        "ashish": "Ashish Rajput",
        "pat": "Pat Nestor",
        "pat nester": "Pat Nestor",
        # Todd Asher (Bloomberg Associates, Deputy Principal Media & Technology) —
        # joined the AI Lab Smart Cities Initiative kickoff 2026-05-01.
        # WhisperX renders his surname inconsistently.
        "todd": "Todd Asher",
        "todd aaron": "Todd Asher",
        "todd sharon": "Todd Asher",
        # Serena McIntosh (Bloomberg Associates, Digital Strategies Manager) —
        # Todd's colleague on the same initiative. Whisper renders as "Serene".
        # Note: distinct from Serena Davy (NTA) — distinguish by org context.
        "serene": "Serena McIntosh",
    },
    "NTA": {
        "cathal": "Cathal Bellew",
        "cahal": "Cathal Bellew",
        "cathal murphy": "Cathal Bellew",
        "carla": "Cathal Bellew",  # WhisperX mishears Irish "Cathal" (KAH-hul)
        "cahill": "Cathal Bellew",
        "cottle": "Cathal Bellew",
        "karl bellews": "Cathal Bellew",  # WhisperX mishears the Irish "Cathal" as "Karl" + adds an 's' to "Bellew"
        "karl bellew": "Cathal Bellew",
        "david spurley": "David Spurway",  # IBM Power EMEA — Whisper hears "Spurway" as "Spurley"
        "declan": "Declan Sheehan",
        "neil": "Neil",
        "mark": "Mark O'Brien Moody",
        "siobhan": "Siobhan Quinn",
    },
    "Diotima": {
        "siobhan": "Siobhan Ryan",
        "jonathan": "Jonathan Dempsey",
        "masa": "Mahsa Mahdinejad",
        "mahsa": "Mahsa Mahdinejad",
        "birva": "Birva Mehta",
    },
    "Paradigm": {
        "guy": "Guy Rackham",
        "sarah": "Sarah Broderick",
        "arjit": "Arijit Sircar",
        "arijit": "Arijit Sircar",
        "arjun": "Arijit Sircar",
        "eddy": "Eddy Moretti",
        "eddie": "Eddy Moretti",
    },
    "ADAPT": {
        "kizzer": "Khizer Ahmed Biyabani",
        "khizer": "Khizer Ahmed Biyabani",
        # NOTE: "ashish" alone is ambiguous in ADAPT — could be Rajput (ADAPT)
        # or Kumar Jha (TBS, often crosses into ADAPT panel/PhD work). Use
        # context (calendar invite or co-attendees) to disambiguate. Default
        # to Rajput when unclear since he's higher-frequency in ADAPT.
        "ashish": "Ashish Rajput",
        "ashish kumar jha": "Ashish Kumar Jha",
        "ashish jha": "Ashish Kumar Jha",
        "declan": "Declan McKibben",
        "sean": "Shunyu Ji",
        "shawn": "Shunyu Ji",
        "arti": "Aarthi Kumar",
        "aarthi": "Aarthi Kumar",
        # Paul Pierotti (EY Ireland AI & Data Partner). Whisper rendered as
        # "Paul Cottrell" on the 2026-05-05 panel prep call.
        "paul cottrell": "Paul Pierotti",
        "paul pierotti": "Paul Pierotti",
    },
    "TBS": {
        "kisito": "Kisito Futonge Nzembayie",
        "kistu": "Kisito Futonge Nzembayie",
        "stu": "Kisito Futonge Nzembayie",
        "daniel": "Daniel Coughlan",
    },
    "DFB": {
        "rob": "Rob Howell",
        "rob hell": "Rob Howell",
        "robert": "Rob Howell",
    },
}
