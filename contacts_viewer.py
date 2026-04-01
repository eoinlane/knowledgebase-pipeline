#!/usr/bin/env python3
"""
contacts_viewer.py — Local web UI for ~/contacts.db with inline editing.
Run: python3 ~/knowledgebase-pipeline/contacts_viewer.py
Open: http://localhost:5100
"""

import json
import re
import sqlite3
import subprocess
from pathlib import Path
from flask import Flask, request, render_template_string, jsonify

DB_PATH          = Path.home() / "contacts.db"
KB_DIR           = Path.home() / "knowledge_base"
MEETINGS_DIR     = KB_DIR / "meetings"
CORRECTIONS_FILE = Path.home() / "kb_corrections.json"
APPLY_SCRIPT     = Path.home() / "knowledgebase-pipeline" / "apply_kb_corrections.py"

KNOWN_ORGS = ["NTA", "DCC", "DFB", "ADAPT", "Diotima", "Paradigm", "TBS"]

REASON_LABELS = {
    "first_name_only": "First name only",
    "name_contained":  "Name contained",
    "edit_distance_1": "1-character difference",
    "edit_distance_2": "2-character difference",
}

app = Flask(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_corrections():
    if not CORRECTIONS_FILE.exists():
        return {"people": {}, "meetings": {}}
    with open(CORRECTIONS_FILE) as f:
        return json.load(f)


def save_corrections(data):
    with open(CORRECTIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def apply_corrections_to_files():
    """Run apply_kb_corrections.py to patch markdown files immediately."""
    try:
        subprocess.run(["python3", str(APPLY_SCRIPT)], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"apply_kb_corrections.py failed: {e.stderr.decode()}")


def org_class(org):
    return f"org-{org}" if org in KNOWN_ORGS else "org-other"


def meeting_people(conn, filename):
    """Return list of raw person names in a meeting."""
    rows = conn.execute(
        "SELECT person_name FROM attendees a JOIN meetings m ON m.id=a.meeting_id "
        "WHERE m.filename=?", (filename,)
    ).fetchall()
    return [r[0] for r in rows]


# ── CSS / shared styles ───────────────────────────────────────────────────────

STYLES = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f5f5f7; color: #1d1d1f; font-size: 14px; }
header { background: #fff; border-bottom: 1px solid #e0e0e0;
         padding: 14px 24px; display: flex; align-items: center; gap: 16px; }
header h1 { font-size: 17px; font-weight: 600; flex: 1; }
header .count { color: #666; font-size: 13px; }
.controls { background: #fff; border-bottom: 1px solid #e0e0e0;
            padding: 10px 24px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
input[type=search], input[type=text] {
  border: 1px solid #ccc; border-radius: 6px; padding: 6px 10px;
  font-size: 13px; outline: none; }
input[type=search] { width: 220px; }
input[type=text] { width: 100%; }
input:focus { border-color: #0071e3; }
select { border: 1px solid #ccc; border-radius: 6px; padding: 6px 10px;
         font-size: 13px; background: #fff; outline: none; cursor: pointer; }
select:focus { border-color: #0071e3; }
.btn { display: inline-block; padding: 5px 12px; border-radius: 6px; font-size: 12px;
       font-weight: 500; cursor: pointer; border: none; }
.btn-primary { background: #0071e3; color: #fff; }
.btn-primary:hover { background: #0058b0; }
.btn-ghost { background: none; color: #0071e3; border: 1px solid #ccc; }
.btn-ghost:hover { background: #f0f0f0; }
.btn-sm { padding: 3px 8px; font-size: 11px; }
.clear-btn { font-size: 12px; color: #0071e3; cursor: pointer;
             background: none; border: none; padding: 4px 6px; }
.clear-btn:hover { text-decoration: underline; }
main { padding: 20px 24px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
thead tr { background: #f0f0f5; }
th { padding: 10px 14px; text-align: left; font-size: 12px; font-weight: 600;
     text-transform: uppercase; letter-spacing: .04em; color: #555;
     cursor: pointer; user-select: none; white-space: nowrap; }
th:hover { color: #0071e3; }
th .sort-arrow { margin-left: 4px; opacity: .4; }
th.sorted .sort-arrow { opacity: 1; color: #0071e3; }
td { padding: 9px 14px; border-top: 1px solid #f0f0f0; vertical-align: middle; }
tr:hover td { background: #fafafa; }
a { color: #0071e3; text-decoration: none; }
a:hover { text-decoration: underline; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
         font-size: 11px; font-weight: 500; }
.org-NTA     { background: #dbeafe; color: #1e40af; }
.org-DCC     { background: #dcfce7; color: #166534; }
.org-DFB     { background: #fee2e2; color: #991b1b; }
.org-Diotima { background: #fef3c7; color: #92400e; }
.org-Paradigm{ background: #ede9fe; color: #5b21b6; }
.org-ADAPT   { background: #fce7f3; color: #9d174d; }
.org-TBS     { background: #f0fdf4; color: #14532d; }
.org-other   { background: #f3f4f6; color: #374151; }
.no-results { text-align: center; padding: 40px; color: #888; }
/* person page */
.back { display: inline-block; margin-bottom: 16px; font-size: 13px; }
.card { background: #fff; border-radius: 10px; padding: 20px 24px;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 16px; }
.card-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.card h2 { font-size: 20px; margin-bottom: 2px; }
.card .subtitle { color: #666; font-size: 13px; margin-bottom: 2px; }
.meta { color: #666; font-size: 13px; margin: 10px 0 14px; }
.meeting-list li { padding: 8px 0; border-bottom: 1px solid #f0f0f0;
                   list-style: none; display: flex; align-items: flex-start; gap: 8px; }
.meeting-list li:last-child { border-bottom: none; }
.meeting-date { color: #888; font-size: 12px; white-space: nowrap; padding-top: 1px; }
.meeting-info { flex: 1; }
.meeting-tags { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 3px; }
/* edit forms */
.edit-panel { background: #f8f8fa; border: 1px solid #e0e0e0; border-radius: 8px;
              padding: 14px 16px; margin-top: 12px; }
.edit-panel label { display: block; font-size: 12px; font-weight: 600;
                    color: #555; margin-bottom: 3px; margin-top: 10px; }
.edit-panel label:first-of-type { margin-top: 0; }
.edit-row { display: flex; gap: 8px; margin-top: 12px; }
.tag-checks { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 4px; }
.tag-checks label { font-size: 12px; font-weight: 400; color: #333;
                    display: flex; align-items: center; gap: 4px; cursor: pointer; }
.people-map { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.people-map-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.people-map-row .arrow { color: #999; }
.people-map-row input { flex: 1; }
.save-msg { font-size: 12px; color: #166534; background: #dcfce7;
            padding: 4px 10px; border-radius: 6px; display: none; }
.corrected { color: #166534; font-style: italic; font-size: 12px; }
"""

# ── index page ────────────────────────────────────────────────────────────────

INDEX_HTML = f"""
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Contacts</title><style>{STYLES}</style></head><body>
<header>
  <h1>Contacts</h1>
  <span class="count" id="result-count"></span>
  <a href="/review" style="font-size:13px;color:#0071e3" id="review-link">Review duplicates</a>
</header>
<div class="controls">
  <input type="search" id="search" placeholder="Search name…" autofocus>
  <select id="org-filter">
    <option value="">All organisations</option>
    {{{{ orgs_options }}}}
  </select>
  <select id="sort-col">
    <option value="meeting_count">Sort: Most meetings</option>
    <option value="last_seen">Sort: Last seen</option>
    <option value="name">Sort: Name</option>
  </select>
  <button class="clear-btn" onclick="clearFilters()">Clear</button>
</div>
<main>
  <table id="contacts-table">
    <thead><tr>
      <th data-col="name">Name <span class="sort-arrow">↕</span></th>
      <th data-col="primary_org">Organisation <span class="sort-arrow">↕</span></th>
      <th data-col="meeting_count">Meetings <span class="sort-arrow">↕</span></th>
      <th data-col="last_seen">Last Seen <span class="sort-arrow">↕</span></th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <p class="no-results" id="no-results" style="display:none">No contacts found.</p>
</main>
<script>
let allData=[], sortCol='meeting_count', sortDir=-1;
async function loadData(){{
  allData=await(await fetch('/api/contacts')).json(); render();
}}
function orgClass(o){{
  return ['NTA','DCC','DFB','Diotima','Paradigm','ADAPT','TBS'].includes(o)?'org-'+o:'org-other';
}}
function render(){{
  const s=document.getElementById('search').value.toLowerCase();
  const o=document.getElementById('org-filter').value;
  let d=allData.filter(r=>r.name.toLowerCase().includes(s)&&(!o||r.primary_org===o));
  d.sort((a,b)=>{{
    let av=a[sortCol]??'', bv=b[sortCol]??'';
    if(sortCol==='meeting_count'){{av=+av;bv=+bv;}}
    return av<bv?sortDir:av>bv?-sortDir:0;
  }});
  document.getElementById('tbody').innerHTML=d.map(r=>`
    <tr>
      <td><a href="/person/${{encodeURIComponent(r.name)}}">${{r.name}}</a>
          ${{r.title?'<span style="color:#888;font-size:11px"> · '+r.title+'</span>':''}}
      </td>
      <td><span class="badge ${{orgClass(r.primary_org)}}">${{r.primary_org||'—'}}</span>
          ${{r.org_detail?'<span style="color:#666;font-size:11px"> '+r.org_detail+'</span>':''}}
      </td>
      <td>${{r.meeting_count}}</td>
      <td>${{r.last_seen||'—'}}</td>
    </tr>`).join('');
  document.getElementById('result-count').textContent=d.length+' contacts';
  document.getElementById('no-results').style.display=d.length?'none':'block';
  document.querySelector('table').style.display=d.length?'':'none';
}}
document.getElementById('search').addEventListener('input',render);
document.getElementById('org-filter').addEventListener('change',render);
document.getElementById('sort-col').addEventListener('change',e=>{{
  sortCol=e.target.value; sortDir=sortCol==='name'?1:-1; render();
}});
document.querySelectorAll('th[data-col]').forEach(th=>{{
  th.addEventListener('click',()=>{{
    const col=th.dataset.col;
    if(sortCol===col)sortDir*=-1; else{{sortCol=col;sortDir=col==='name'?1:-1;}}
    document.querySelectorAll('th').forEach(t=>t.classList.remove('sorted'));
    th.classList.add('sorted');
    th.querySelector('.sort-arrow').textContent=sortDir===1?'↑':'↓';
    render();
  }});
}});
function clearFilters(){{
  document.getElementById('search').value='';
  document.getElementById('org-filter').value='';
  render();
}}
loadData();
</script></body></html>
"""

# ── person page ───────────────────────────────────────────────────────────────

PERSON_HTML = f"""
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{{{ display_name }}}} — Contacts</title><style>{STYLES}</style></head><body>
<header><h1>{{{{ display_name }}}}</h1></header>
<main>
  <a class="back" href="/">← All contacts</a>
  <div class="card">
    <div class="card-header">
      <div>
        <h2 id="display-name">{{{{ display_name }}}}</h2>
        {{%- if person.title or person.org_detail %}}
        <div class="subtitle" id="display-subtitle">
          {{{{ person.title or '' }}}}{{{{ ' · ' if person.title and person.org_detail else '' }}}}{{{{ person.org_detail or '' }}}}
        </div>
        {{%- endif %}}
      </div>
      <button class="btn btn-ghost btn-sm" onclick="togglePersonEdit()">Edit</button>
    </div>
    <div class="meta">
      <span class="badge {{{{ org_class }}}}">{{{{ person.primary_org or 'Unknown org' }}}}</span>
      &nbsp; {{{{ person.meeting_count }}}} meetings &nbsp;·&nbsp;
      Last seen: {{{{ person.last_seen or 'unknown' }}}}
      {{%- if person.resolved_name %}}
      &nbsp;·&nbsp; <span class="corrected">✓ name corrected</span>
      {{%- endif %}}
    </div>

    <!-- person edit panel -->
    <div class="edit-panel" id="person-edit" style="display:none">
      <label>Display name</label>
      <input type="text" id="edit-name" value="{{{{ display_name }}}}">
      <label>Title</label>
      <input type="text" id="edit-title" value="{{{{ person.title or '' }}}}" placeholder="e.g. Chief Fire Officer">
      <label>Organisation detail</label>
      <input type="text" id="edit-org" value="{{{{ person.org_detail or '' }}}}" placeholder="e.g. Dublin Fire Brigade">
      <div class="edit-row">
        <button class="btn btn-primary btn-sm" onclick="savePerson()">Save</button>
        <button class="btn btn-ghost btn-sm" onclick="togglePersonEdit()">Cancel</button>
        <span class="save-msg" id="person-save-msg">Saved</span>
      </div>
    </div>

    {{%- if meetings %}}
    <ul class="meeting-list" style="margin-top:14px">
      {{%- for m in meetings %}}
      <li id="meeting-{{{{ loop.index }}}}">
        <span class="meeting-date">{{{{ m.date }}}}</span>
        <div class="meeting-info">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            <span class="badge {{{{ badge_class(m.category) }}}}">{{{{ m.category }}}}</span>
            {{%- for tag in (m.tags or []) %}}
            <span class="badge {{{{ badge_class(tag) }}}}">{{{{ tag }}}}</span>
            {{%- endfor %}}
            <span>{{{{ m.title }}}}</span>
            <button class="btn btn-ghost btn-sm" style="margin-left:auto"
                    onclick="toggleMeetingEdit('{{{{ loop.index }}}}','{{{{ m.filename }}}}')">Edit</button>
          </div>
          <!-- meeting edit panel -->
          <div class="edit-panel" id="medit-{{{{ loop.index }}}}" style="display:none;margin-top:8px">
            <label>Topic</label>
            <input type="text" id="mtopic-{{{{ loop.index }}}}" value="{{{{ m.title }}}}">
            <label>Tags (secondary categories)</label>
            <div class="tag-checks" id="mtags-{{{{ loop.index }}}}">
              {{%- for org in all_orgs %}}
              <label>
                <input type="checkbox" value="{{{{ org }}}}"
                  {{{{ 'checked' if org in (m.tags or []) else '' }}}}>
                {{{{ org }}}}
              </label>
              {{%- endfor %}}
            </div>
            <label>People in this meeting → correct name</label>
            <div class="people-map" id="mpeople-{{{{ loop.index }}}}">
              {{%- for pname in m.people %}}
              <div class="people-map-row">
                <span style="min-width:100px;color:#444">{{{{ pname }}}}</span>
                <span class="arrow">→</span>
                <input type="text" placeholder="corrected name (leave blank to keep)"
                       id="pcorr-{{{{ loop.index }}}}-{{{{ loop.index0 }}}}"
                       data-raw="{{{{ pname }}}}"
                       value="{{{{ m.people_corrections.get(pname, '') }}}}">
              </div>
              {{%- endfor %}}
            </div>
            <div class="edit-row">
              <button class="btn btn-primary btn-sm"
                      onclick="saveMeeting('{{{{ loop.index }}}}','{{{{ m.filename }}}}')">Save</button>
              <button class="btn btn-ghost btn-sm"
                      onclick="toggleMeetingEdit('{{{{ loop.index }}}}','{{{{ m.filename }}}}')">Cancel</button>
              <span class="save-msg" id="msave-{{{{ loop.index }}}}">Saved — markdown patched</span>
            </div>
          </div>
        </div>
      </li>
      {{%- endfor %}}
    </ul>
    {{%- else %}}
    <p style="color:#888;margin-top:14px">No meetings found.</p>
    {{%- endif %}}
  </div>
</main>
<script>
const RAW_NAME = {{{{ raw_name_json }}}};

function togglePersonEdit() {{
  const p = document.getElementById('person-edit');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
}}

function toggleMeetingEdit(idx, filename) {{
  const p = document.getElementById('medit-' + idx);
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
}}

async function savePerson() {{
  const payload = {{
    raw_name:   RAW_NAME,
    name:       document.getElementById('edit-name').value.trim(),
    title:      document.getElementById('edit-title').value.trim(),
    org_detail: document.getElementById('edit-org').value.trim(),
  }};
  const r = await fetch('/api/person/edit', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }});
  if (r.ok) {{
    document.getElementById('display-name').textContent = payload.name || RAW_NAME;
    const msg = document.getElementById('person-save-msg');
    msg.style.display = 'inline'; setTimeout(() => msg.style.display='none', 3000);
  }}
}}

async function saveMeeting(idx, filename) {{
  // Collect tags
  const tagBoxes = document.querySelectorAll('#mtags-' + idx + ' input[type=checkbox]');
  const tags = Array.from(tagBoxes).filter(c => c.checked).map(c => c.value);

  // Collect people corrections
  const corrInputs = document.querySelectorAll('[id^="pcorr-' + idx + '-"]');
  const people_corrections = {{}};
  corrInputs.forEach(inp => {{
    const val = inp.value.trim();
    if (val) people_corrections[inp.dataset.raw] = val;
  }});

  const payload = {{
    filename,
    topic:              document.getElementById('mtopic-' + idx).value.trim(),
    tags,
    people_corrections,
  }};
  const r = await fetch('/api/meeting/edit', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }});
  if (r.ok) {{
    const msg = document.getElementById('msave-' + idx);
    msg.style.display = 'inline'; setTimeout(() => msg.style.display='none', 4000);
  }}
}}
</script></body></html>
"""


# ── review page ──────────────────────────────────────────────────────────────

REVIEW_HTML = f"""
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review Duplicates — Contacts</title><style>{STYLES}
.suggestion {{ background:#fff; border-radius:10px; padding:18px 20px;
              box-shadow:0 1px 3px rgba(0,0,0,.08); margin-bottom:12px; }}
.suggestion-header {{ display:flex; align-items:center; gap:10px; margin-bottom:12px; }}
.confidence {{ font-size:11px; font-weight:600; padding:2px 8px; border-radius:10px; }}
.conf-high   {{ background:#dcfce7; color:#166534; }}
.conf-medium {{ background:#fef3c7; color:#92400e; }}
.conf-low    {{ background:#f3f4f6; color:#374151; }}
.person-card {{ display:flex; gap:16px; align-items:flex-start; }}
.person-box  {{ flex:1; background:#f8f8fa; border-radius:8px; padding:12px 14px; }}
.person-box h3 {{ font-size:15px; margin-bottom:4px; }}
.person-box .sub {{ font-size:12px; color:#666; }}
.vs {{ font-size:18px; color:#ccc; font-weight:300; padding-top:20px; }}
.action-row {{ display:flex; gap:8px; margin-top:14px; align-items:center; flex-wrap:wrap; }}
.reason-pill {{ font-size:11px; color:#555; background:#f0f0f5; padding:2px 8px;
               border-radius:10px; }}
.merge-select {{ font-size:12px; border:1px solid #ccc; border-radius:6px;
                padding:4px 8px; background:#fff; }}
.empty {{ text-align:center; padding:60px; color:#888; }}
.nav-tabs {{ display:flex; gap:0; border-bottom:2px solid #e0e0e0; margin-bottom:20px; }}
.nav-tab  {{ padding:8px 18px; font-size:13px; font-weight:500; color:#666;
             cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-2px; }}
.nav-tab.active {{ color:#0071e3; border-bottom-color:#0071e3; }}
</style></head><body>
<header>
  <h1>Review Duplicates</h1>
  <span class="count">{{{{ pending }}}} pending</span>
</header>
<main>
  <a class="back" href="/">← All contacts</a>

  {{%- if suggestions %}}
  <div id="suggestions">
  {{%- for s in suggestions %}}
  <div class="suggestion" id="s-{{{{ s.id }}}}">
    <div class="suggestion-header">
      <span class="confidence {{{{ 'conf-high' if s.confidence >= 0.8 else 'conf-medium' if s.confidence >= 0.6 else 'conf-low' }}}}">
        {{{{ "%.0f%%" % (s.confidence * 100) }}}} confidence
      </span>
      <span class="reason-pill">{{{{ reason_label(s.reason) }}}}</span>
    </div>
    <div class="person-card">
      <div class="person-box">
        <h3><a href="/person/{{{{ s.canonical_name }}}}">{{{{ s.canonical_name }}}}</a></h3>
        <div class="sub">
          <span class="badge {{{{ badge_class(s.canonical_org) }}}}">{{{{ s.canonical_org or '?' }}}}</span>
          &nbsp; {{{{ s.canonical_count }}}} meetings
        </div>
      </div>
      <div class="vs">?</div>
      <div class="person-box">
        <h3><a href="/person/{{{{ s.alias_name }}}}">{{{{ s.alias_name }}}}</a></h3>
        <div class="sub">
          <span class="badge {{{{ badge_class(s.alias_org) }}}}">{{{{ s.alias_org or '?' }}}}</span>
          &nbsp; {{{{ s.alias_count }}}} meetings
        </div>
      </div>
    </div>
    <div class="action-row">
      <span style="font-size:12px;color:#555">Keep as canonical:</span>
      <select class="merge-select" id="canonical-{{{{ s.id }}}}">
        <option value="{{{{ s.canonical_raw }}}}">{{{{ s.canonical_name }}}}</option>
        <option value="{{{{ s.alias_raw }}}}">{{{{ s.alias_name }}}}</option>
      </select>
      <button class="btn btn-primary btn-sm"
              onclick="merge({{{{ s.id }}}}, '{{{{ s.canonical_raw }}}}', '{{{{ s.alias_raw }}}}')">
        Same person — merge
      </button>
      <button class="btn btn-ghost btn-sm"
              onclick="dismiss({{{{ s.id }}}}, '{{{{ s.canonical_raw }}}}', '{{{{ s.alias_raw }}}}')">
        Different people — dismiss
      </button>
    </div>
  </div>
  {{%- endfor %}}
  </div>
  {{%- else %}}
  <div class="empty">
    <p style="font-size:18px;margin-bottom:8px">✓ All caught up</p>
    <p>No duplicate suggestions pending review.</p>
  </div>
  {{%- endif %}}
</main>
<script>
async function merge(id, canonicalRaw, aliasRaw) {{
  const sel = document.getElementById('canonical-' + id);
  const chosenCanonical = sel.value;
  const chosenAlias = chosenCanonical === canonicalRaw ? aliasRaw : canonicalRaw;
  const r = await fetch('/api/merge', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{ suggestion_id: id, canonical_raw: chosenCanonical, alias_raw: chosenAlias }})
  }});
  if (r.ok) {{
    const el = document.getElementById('s-' + id);
    el.style.opacity = '0.4';
    el.style.pointerEvents = 'none';
    el.querySelector('.action-row').innerHTML =
      '<span style="color:#166534;font-size:12px">✓ Merged — ' + chosenAlias + ' → ' + chosenCanonical + '</span>';
  }}
}}

async function dismiss(id, name1, name2) {{
  const r = await fetch('/api/dismiss', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{ suggestion_id: id, name1, name2 }})
  }});
  if (r.ok) {{
    document.getElementById('s-' + id).remove();
    const pending = document.querySelectorAll('.suggestion').length;
    document.querySelector('.count').textContent = pending + ' pending';
    if (!pending) location.reload();
  }}
}}
</script></body></html>
"""

# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    conn = get_db()
    orgs = [r[0] for r in conn.execute(
        "SELECT DISTINCT primary_org FROM people WHERE primary_org != '' ORDER BY primary_org"
    ).fetchall()]
    conn.close()
    opts = "\n".join(f'<option value="{o}">{o}</option>' for o in orgs)
    return INDEX_HTML.replace("{{ orgs_options }}", opts)


@app.route("/api/contacts")
def api_contacts():
    conn = get_db()
    rows = conn.execute("""
        SELECT
            COALESCE(resolved_name, name)  AS name,
            name                           AS raw_name,
            primary_org,
            meeting_count,
            last_seen,
            COALESCE(resolved_slug, slug)  AS slug,
            has_file,
            COALESCE(title, '')            AS title,
            COALESCE(org_detail, '')       AS org_detail,
            CASE WHEN resolved_name IS NOT NULL THEN 1 ELSE 0 END AS is_resolved
        FROM people ORDER BY meeting_count DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/person/<name>")
def person(name):
    conn = get_db()
    p = conn.execute(
        "SELECT * FROM people WHERE resolved_name=? OR name=? LIMIT 1", (name, name)
    ).fetchone()
    if not p:
        conn.close()
        return "Person not found", 404

    raw_name     = p["name"]
    display_name = p["resolved_name"] or p["name"]

    # Load corrections for people-in-meeting corrections
    corrections  = load_corrections()
    meeting_corrs = corrections.get("meetings", {})

    rows = conn.execute("""
        SELECT m.filename, m.title, m.date, m.category,
               COALESCE(m.tags, '[]') as tags_json
        FROM attendees a
        JOIN meetings m ON m.id = a.meeting_id
        WHERE a.person_name = ?
        ORDER BY m.date DESC
    """, (raw_name,)).fetchall()

    meetings = []
    for row in rows:
        fname = row["filename"]
        try:
            tags = json.loads(row["tags_json"])
        except Exception:
            tags = []
        mcorr = meeting_corrs.get(fname, {})
        people_corrections = mcorr.get("people_corrections", {})
        people = meeting_people(conn, fname)
        meetings.append({
            "filename":          fname,
            "title":             mcorr.get("topic") or row["title"],
            "date":              row["date"],
            "category":          row["category"],
            "tags":              mcorr.get("tags", tags),
            "people":            people,
            "people_corrections": people_corrections,
        })

    conn.close()

    oc = org_class(p["primary_org"] or "")

    def badge_class(o):
        return f"org-{o}" if o in KNOWN_ORGS else "org-other"

    return render_template_string(
        PERSON_HTML,
        person=p,
        display_name=display_name,
        raw_name_json=json.dumps(raw_name),
        meetings=meetings,
        org_class=oc,
        badge_class=badge_class,
        all_orgs=KNOWN_ORGS,
        kb_dir=str(KB_DIR),
    )


@app.route("/review")
def review():
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT * FROM merge_suggestions
            WHERE status = 'pending'
            ORDER BY confidence DESC
        """).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()

    def reason_label(r):
        for k, v in REASON_LABELS.items():
            if r and r.startswith(k):
                return v
        if r and r.startswith("similar_"):
            pct = r.replace("similar_", "").replace("pct", "")
            return f"{pct}% name similarity"
        return r or "unknown"

    def badge_cls(o):
        return f"org-{o}" if o in KNOWN_ORGS else "org-other"

    return render_template_string(
        REVIEW_HTML,
        suggestions=rows,
        pending=len(rows),
        reason_label=reason_label,
        badge_class=badge_cls,
    )


@app.route("/api/merge", methods=["POST"])
def api_merge():
    data         = request.json
    sid          = data.get("suggestion_id")
    canonical_raw = data.get("canonical_raw", "").strip()
    alias_raw    = data.get("alias_raw", "").strip()

    if not canonical_raw or not alias_raw:
        return jsonify({"error": "canonical_raw and alias_raw required"}), 400

    conn = get_db()

    # Get display names
    canonical_row = conn.execute(
        "SELECT COALESCE(resolved_name, name) as display FROM people WHERE name=?",
        (canonical_raw,)
    ).fetchone()
    canonical_display = canonical_row["display"] if canonical_row else canonical_raw

    # Reassign all attendee records from alias → canonical raw name
    conn.execute(
        "UPDATE attendees SET person_name=? WHERE person_name=?",
        (canonical_raw, alias_raw)
    )

    # Update people table: mark alias as merged
    conn.execute(
        "UPDATE people SET resolved_name=?, resolved_slug=? WHERE name=?",
        (canonical_display,
         re.sub(r"[^a-z0-9-]", "", canonical_display.lower().replace(" ", "-")),
         alias_raw)
    )

    # Merge meeting counts into canonical
    total = conn.execute(
        "SELECT COUNT(DISTINCT meeting_id) FROM attendees WHERE person_name=?",
        (canonical_raw,)
    ).fetchone()[0]
    conn.execute("UPDATE people SET meeting_count=? WHERE name=?", (total, canonical_raw))

    # Mark suggestion as merged
    if sid:
        conn.execute(
            "UPDATE merge_suggestions SET status='merged' WHERE id=?", (sid,)
        )

    conn.commit()
    conn.close()

    # Write to corrections file so it survives rebuilds
    corrections = load_corrections()
    entry = corrections["people"].setdefault(alias_raw, {})
    entry["name"] = canonical_display
    save_corrections(corrections)

    apply_corrections_to_files()

    return jsonify({"ok": True, "canonical": canonical_display, "alias": alias_raw})


@app.route("/api/dismiss", methods=["POST"])
def api_dismiss():
    data = request.json
    sid  = data.get("suggestion_id")
    n1   = data.get("name1", "").strip()
    n2   = data.get("name2", "").strip()

    if not n1 or not n2:
        return jsonify({"error": "name1 and name2 required"}), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO dismissed_pairs (name1, name2) VALUES (?,?)", (n1, n2)
        )
        conn.execute(
            "INSERT OR IGNORE INTO dismissed_pairs (name1, name2) VALUES (?,?)", (n2, n1)
        )
        if sid:
            conn.execute(
                "UPDATE merge_suggestions SET status='dismissed' WHERE id=?", (sid,)
            )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()

    return jsonify({"ok": True})


@app.route("/api/person/edit", methods=["POST"])
def api_person_edit():
    data     = request.json
    raw_name = data.get("raw_name", "").strip()
    new_name = data.get("name", "").strip()
    title    = data.get("title", "").strip()
    org_detail = data.get("org_detail", "").strip()

    if not raw_name:
        return jsonify({"error": "raw_name required"}), 400

    # ── Update corrections file ────────────────────────────────────────────────
    corrections = load_corrections()
    entry = corrections["people"].setdefault(raw_name, {})
    if new_name and new_name != raw_name:
        entry["name"] = new_name
    elif "name" in entry and new_name == raw_name:
        del entry["name"]
    if title:
        entry["title"] = title
    if org_detail:
        entry["org"] = org_detail
    save_corrections(corrections)

    # ── Update DB immediately ──────────────────────────────────────────────────
    conn = get_db()
    new_slug = re.sub(r"[^a-z0-9-]", "", (new_name or raw_name).lower()
                      .replace(" ", "-").replace("'", ""))
    conn.execute("""
        UPDATE people
        SET resolved_name = CASE WHEN ? != '' THEN ? ELSE resolved_name END,
            resolved_slug = CASE WHEN ? != '' THEN ? ELSE resolved_slug END,
            title         = CASE WHEN ? != '' THEN ? ELSE title END,
            org_detail    = CASE WHEN ? != '' THEN ? ELSE org_detail END
        WHERE name = ?
    """, (new_name, new_name, new_name, new_slug,
          title, title, org_detail, org_detail, raw_name))
    conn.commit()
    conn.close()

    # ── Patch markdown files ───────────────────────────────────────────────────
    apply_corrections_to_files()

    return jsonify({"ok": True})


@app.route("/api/meeting/edit", methods=["POST"])
def api_meeting_edit():
    data     = request.json
    filename = data.get("filename", "").strip()
    topic    = data.get("topic", "").strip()
    tags     = data.get("tags", [])
    people_corrections = data.get("people_corrections", {})

    if not filename:
        return jsonify({"error": "filename required"}), 400

    # ── Update corrections file ────────────────────────────────────────────────
    corrections = load_corrections()
    entry = corrections["meetings"].setdefault(filename, {})
    if topic:
        entry["topic"] = topic
    if tags is not None:
        entry["tags"] = tags
    if people_corrections:
        entry["people_corrections"] = people_corrections
    elif "people_corrections" in entry and not people_corrections:
        del entry["people_corrections"]
    save_corrections(corrections)

    # ── Update DB meetings table ───────────────────────────────────────────────
    conn = get_db()
    conn.execute("""
        UPDATE meetings SET
            topic = CASE WHEN ? != '' THEN ? ELSE topic END,
            tags  = ?
        WHERE filename = ?
    """, (topic, topic, json.dumps(tags), filename))

    # If people corrections exist, update attendees
    for old_name, new_name in people_corrections.items():
        if not new_name:
            continue
        # Check if person exists in DB
        existing = conn.execute(
            "SELECT id FROM people WHERE name=? OR resolved_name=?", (new_name, new_name)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT OR IGNORE INTO people (name, slug) VALUES (?,?)",
                (new_name, re.sub(r"[^a-z0-9-]", "", new_name.lower().replace(" ", "-")))
            )
        # Update people corrections in the people table
        conn.execute("""
            UPDATE people SET resolved_name=?
            WHERE name=? AND (resolved_name IS NULL OR resolved_name=?)
        """, (new_name, old_name, old_name))

    conn.commit()
    conn.close()

    # ── Patch markdown files ───────────────────────────────────────────────────
    apply_corrections_to_files()

    return jsonify({"ok": True})


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        print("Run: python3 ~/knowledgebase-pipeline/build_contacts_db.py")
        raise SystemExit(1)
    print("Contacts viewer: http://localhost:5100")
    app.run(port=5100, debug=False)
