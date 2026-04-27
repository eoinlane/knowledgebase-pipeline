"""
Category-specific name expansion tables.
Maps WhisperX mishearings and short names to full names.
Used by identify_speakers.py for speaker identification.
"""

CATEGORY_NAME_EXPANSIONS = {
    "DCC": {
        "chris": "Christopher Kelly",
        "christopher": "Christopher Kelly",
        # WhisperX hears the Irish English "grand" (= "fine/good") as the proper
        # noun "Grant" and labels it as a speaker. In DCC GenAI Lab context the
        # speaker is Christopher Kelly (day-to-day DCC contact, hands-on with
        # the team's indexed datasets).
        "grant": "Christopher Kelly",
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
    },
    "NTA": {
        "cathal": "Cathal Bellew",
        "cahal": "Cathal Bellew",
        "cathal murphy": "Cathal Bellew",
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
        "ashish": "Ashish Rajput",
        "declan": "Declan McKibben",
        "sean": "Shunyu Ji",
        "shawn": "Shunyu Ji",
    },
    "TBS": {
        "kisito": "Kisito Futonge Nzembayie",
        "kistu": "Kisito Futonge Nzembayie",
        "stu": "Kisito Futonge Nzembayie",
        "daniel": "Daniel Coughlan",
        # WhisperX hears the Irish English "grand" (= "fine/good") as the proper
        # noun "Grant" and assigns it as a speaker label. In TBS context that
        # speaker is Neil Dunne — runs MSc Accounting & Analytics, BU7852.
        "grant": "Neil Dunne",
    },
    "DFB": {
        "rob": "Rob Howell",
        "rob hell": "Rob Howell",
        "robert": "Rob Howell",
    },
}
