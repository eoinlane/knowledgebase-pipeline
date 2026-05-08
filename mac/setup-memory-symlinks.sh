#!/bin/bash
# setup-memory-symlinks.sh
#
# Tier 2 of the KB ↔ project-folder integration: symlinks the cross-cutting
# subset of memory (Eoin's profile, feedback rules, client + person dossiers)
# into each project folder's Claude memory directory, so dossiers and
# behavioural rules load consistently when Claude is opened in any project
# folder rather than only in ~/knowledgebase-pipeline/.
#
# Pipeline-specific memory (voice catalog, calendar matcher quirks, GPU
# infrastructure, graph-layer audit, test suite) is intentionally NOT
# symlinked — that stuff is only useful inside this repo.
#
# Idempotent. Safe to re-run when new memory files are added (just update
# CROSS_CUTTING_FILES below first).
#
# Mechanism: for each project folder, the script populates its claude memory
# directory at ~/.claude/projects/<sanitised-cwd>/memory/ with per-file
# symlinks pointing at the source files in the pipeline's memory directory,
# plus a generated slim MEMORY.md index that lists only the cross-cutting
# files.

set -euo pipefail

PIPELINE_MEMORY="$HOME/.claude/projects/-Users-eoin-knowledgebase-pipeline/memory"

if [ ! -d "$PIPELINE_MEMORY" ]; then
    echo "ERROR: pipeline memory not found at $PIPELINE_MEMORY" >&2
    exit 1
fi

# ── Cross-cutting memory files ───────────────────────────────────────────────
# Files that should load in any project folder. Update this list when a new
# cross-cutting memory file is added (user profile, feedback rule, client or
# person dossier, cross-project concept) and re-run the script.
CROSS_CUTTING_FILES=(
    # User profile
    user_eoin_career_background.md
    user_family.md

    # Behavioural rules (apply everywhere)
    feedback_always_push.md
    feedback_apply_fixes_to_existing_data.md
    feedback_autonomy.md
    feedback_cathal_mishearings.md
    feedback_email_accounts.md
    feedback_eoin_is_always_eoin.md
    feedback_fix_not_workaround.md
    feedback_secrets.md
    feedback_shorthand.md
    feedback_shunyu.md
    feedback_unknown_jara.md

    # Client dossiers
    project_aurum.md
    project_adapt.md
    project_client_mapping.md
    project_cross_project_people.md
    project_dcc.md
    project_dcc_building_control.md
    project_dcc_property_register.md
    project_diotima.md
    project_lcc_alan_dooley.md
    project_nta.md
    project_nta_projects.md
    project_paradigm.md
    project_tbs.md
    project_tcd_teaching.md

    # Person dossiers
    project_aarthi_kumar.md
    project_alex_mckenzie.md
    project_ashish_kumar_jha.md
    project_neil_dunne_tbs.md
    project_paul_pierotti.md
    project_serena_mcintosh.md
    project_todd_asher.md

    # Cross-project concepts
    project_digital_twin.md

    # Methodology
    reference_second_brain.md
)

# ── Project folders to receive the shared memory ─────────────────────────────
# Each entry is the path Claude Code would compute for a folder's memory.
# Format: ~/.claude/projects/<sanitised-cwd>/. Sanitisation rule is "/" → "-",
# " " → "-", with a leading "-" prefix. Hardcoded for explicitness rather
# than auto-discovered.
PROJECT_MEMORY_DIRS=(
    # Umbrella folders
    "$HOME/.claude/projects/-Users-eoin-Documents-NTA/memory"
    "$HOME/.claude/projects/-Users-eoin-Documents-TBS/memory"
    "$HOME/.claude/projects/-Users-eoin-Documents-patents/memory"

    # Leaf folders
    "$HOME/.claude/projects/-Users-eoin-Documents-NTA-active-travel/memory"
    "$HOME/.claude/projects/-Users-eoin-Documents-NTA-tenders/memory"
    "$HOME/.claude/projects/-Users-eoin-Documents-TBS-DAA/memory"
    "$HOME/.claude/projects/-Users-eoin-Documents-TBS-BU7852/memory"
    "$HOME/.claude/projects/-Users-eoin-Documents-TBS-BUU33803--Business-Analytics-for-AY-2026-27/memory"
    "$HOME/.claude/projects/-Users-eoin-Documents-patents-diotima/memory"
    "$HOME/.claude/projects/-Users-eoin-Documents-whitepaper/memory"
    "$HOME/.claude/projects/-Users-eoin-Documents-aurum/memory"

    # Standalone workspaces
    "$HOME/.claude/projects/-Users-eoin-paradigm/memory"
)

# ── Generate slim MEMORY.md index for project folders ────────────────────────
generate_slim_memory_md() {
    local target="$1"
    cat > "$target" <<'MARKDOWN'
# Eoin Lane — shared memory (cross-cutting subset)

> Source of truth: `~/.claude/projects/-Users-eoin-knowledgebase-pipeline/memory/`.
> Files here are symlinks. Pipeline-specific memory (voice catalog, watcher
> infrastructure, calendar matcher quirks, graph-layer audit) is NOT linked
> in — that stuff only loads inside `~/knowledgebase-pipeline/`.

## About Eoin
- AI consultant/advisor, Dublin, Ireland. Own company: **Noval Consultancy**, placed via Org Group / Morgan McKinley.
- Clients: NTA, DCC, Diotima, Paradigm, ADAPT, TBS, LCC. Plus own product **Aurum**.
- See [user_eoin_career_background](user_eoin_career_background.md) and [user_family](user_family.md).

## Behavioural rules (always apply)
- [Always commit and push](feedback_always_push.md)
- [Apply fixes proactively to existing data](feedback_apply_fixes_to_existing_data.md)
- [Operate autonomously, don't ask permission on routine work](feedback_autonomy.md)
- [Fix root causes, not workarounds](feedback_fix_not_workaround.md)
- [Don't commit secrets](feedback_secrets.md)
- [Shorthand / writing style preferences](feedback_shorthand.md)
- [Cathal is always Cathal Bellew (normalise mishearings)](feedback_cathal_mishearings.md)
- [Eoin is always Eoin Lane (normalise variants)](feedback_eoin_is_always_eoin.md)
- [Shunyu Ji = Shawn = Sean (DCC GenAI Lab)](feedback_shunyu.md)
- [Email accounts: ADAPT and NTA Gmail are separate](feedback_email_accounts.md)
- [Jara = unidentified NTA contact, possibly Dermot O'Gara](feedback_unknown_jara.md)

## Clients (per-engagement roll-ups)
- [Aurum](project_aurum.md) — **Eoin's own product**, wealth management on a tri-state money model. Three repos: aurum (engine), aurum-company (pitch), aurum-demo (public).
- [DCC](project_dcc.md) — Dublin City Council, GenAI Lab, Building Control, Fire Brigade. No project folder, meeting-driven.
- [NTA](project_nta.md) — National Transport Authority. Tender drafts in `~/Documents/NTA/tenders/` are local-only.
- [TBS / TCD](project_tbs.md) — Trinity Business School (DAA tender + module teaching).
- [Diotima](project_diotima.md) — patent + spin-out work; private repo `eoinlane/diotima-patent`.
- [Paradigm](project_paradigm.md) — `eoinlane/paradigm` is canonical (consolidates older paradiym_poc + CRM_plus_UI).
- [ADAPT](project_adapt.md) — research network, EY-ADAPT PhD sponsorship, panel discussion series.
- [LCC](project_lcc_alan_dooley.md) — Limerick County Council via Alan Dooley.

## People (cross-project)
- [Cross-project people](project_cross_project_people.md)
- [Client mapping](project_client_mapping.md)
- [Alex McKenzie (NTA)](project_alex_mckenzie.md)
- [Aarthi Kumar (CK Delta)](project_aarthi_kumar.md)
- [Ashish Kumar Jha (TBS / TCD, runs AIS Ireland panel chapter)](project_ashish_kumar_jha.md) — distinct from Ashish Rajput (ADAPT).
- [Neil Dunne (TBS)](project_neil_dunne_tbs.md)
- [Paul Pierotti (EY Ireland Partner, AI & Data)](project_paul_pierotti.md)
- [Serena McIntosh (Bloomberg Associates)](project_serena_mcintosh.md) — distinct from Serena Davy (NTA).
- [Todd Asher (Bloomberg Associates)](project_todd_asher.md)

## Sub-project / cross-cutting concept context
- [DCC Building Control](project_dcc_building_control.md)
- [DCC Property Register](project_dcc_property_register.md)
- [NTA projects detail](project_nta_projects.md)
- [TCD teaching](project_tcd_teaching.md)
- [Digital twin (cross-project concept)](project_digital_twin.md)

## Methodology
- [Building a Second Brain (CODE framework)](reference_second_brain.md)
MARKDOWN
}

# ── Apply ────────────────────────────────────────────────────────────────────
echo "Cross-cutting files: ${#CROSS_CUTTING_FILES[@]}"
echo "Project folders:     ${#PROJECT_MEMORY_DIRS[@]}"
echo ""

for memdir in "${PROJECT_MEMORY_DIRS[@]}"; do
    parent_project=$(dirname "$memdir" | xargs basename)
    mkdir -p "$memdir"

    # Refresh symlinks: remove any existing memory symlinks for cross-cutting
    # files, then re-create. Real files (project-folder-local memory) are
    # left alone.
    for f in "${CROSS_CUTTING_FILES[@]}"; do
        target="$memdir/$f"
        source="$PIPELINE_MEMORY/$f"
        if [ ! -f "$source" ]; then
            echo "  [skip] $f — not found in pipeline memory"
            continue
        fi
        if [ -L "$target" ] || [ ! -e "$target" ]; then
            ln -sf "$source" "$target"
        else
            echo "  [keep] $target — exists as real file, not overwriting"
        fi
    done

    # Slim MEMORY.md (real file, regenerated each run)
    generate_slim_memory_md "$memdir/MEMORY.md"
    echo "  [done] $parent_project"
done

echo ""
echo "Done. ${#CROSS_CUTTING_FILES[@]} symlinks + 1 slim MEMORY.md per project folder."
