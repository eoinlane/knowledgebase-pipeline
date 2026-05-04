# Mac launchd plists

Source-of-truth copies of `~/Library/LaunchAgents/com.eoin.*.plist`. Tracked in git so a Mac wipe (or a misconfiguration like the 2026-05-04 `Weekday=2` Tuesday-vs-Monday bug) is recoverable.

These are install artefacts — the live versions live at `~/Library/LaunchAgents/`. The shell scripts they invoke are tracked separately under `mac/launchd/*.sh`.

## Install / reinstall on a new Mac

```bash
for f in mac/launchd/plists/com.eoin.*.plist; do
    cp "$f" ~/Library/LaunchAgents/
done

for f in ~/Library/LaunchAgents/com.eoin.*.plist; do
    launchctl bootstrap gui/$(id -u) "$f"
done
```

## Update a single agent

When editing a plist (e.g. changing schedule), edit BOTH the live copy
in `~/Library/LaunchAgents/` AND the tracked copy here, then reload:

```bash
launchctl bootout gui/$(id -u)/com.eoin.<label> 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.eoin.<label>.plist
```

## Schedule conventions

`StartCalendarInterval` uses **Sunday=0** for `Weekday`:

| Weekday | Day |
|---|---|
| 0 or 7 | Sunday |
| 1 | Monday |
| 2 | Tuesday |
| 3 | Wednesday |
| 4 | Thursday |
| 5 | Friday |
| 6 | Saturday |

The `check-kb-orphans` agent had `Weekday=2` for the first week thinking it meant Monday — fired on Tuesdays instead. Caught 2026-05-04. Fixed to `Weekday=1`.

## Catalog

| Plist | What it runs | Schedule |
|---|---|---|
| `backup-claude-memory.plist` | `~/.local/bin/backup-claude-memory.sh` | WatchPaths — fires on changes to `~/.claude/projects/*/memory/` |
| `backup-voice-state.plist` | `mac/launchd/backup-voice-state.sh` | Daily 03:23 — voice catalog + speaker mappings rsync from Ubuntu |
| `check-kb-orphans.plist` | `mac/launchd/check-kb-orphans.sh` | **Mondays 09:17** (Weekday=1) — verify orphan-cleanup is firing |
| `eod-reconciliation.plist` | `mac/launchd/eod-reconciliation.sh` | Daily 19:00 — re-export calendar, rebuild, diff today's matches |
| `export-notes-audio.plist` | (Mac Shortcut, not tracked here) | WatchPaths — Apple Notes recording → My Notes Audio export |
| `pipeline-health-check.plist` | `mac/launchd/pipeline-health-check.sh` | Hourly + RunAtLoad — Ubuntu/ollama-box/services up; backlog growing? |
| `process-inbox.plist` | `mac/process_inbox.py` | WatchPaths — `~/inbox/` for documents (PDF/DOCX/PPTX/EML/etc) |
| `rebuild-knowledge-base.plist` | `~/.local/bin/rebuild-knowledge-base.sh` | Daily 04:00 — full nightly KB rebuild |
| `sync-inbox-audio.plist` | `mac/launchd/sync-inbox-audio.sh` | WatchPaths — `~/inbox/` audio → Ubuntu, then delete local |
| `sync-knowledge-base.plist` | `mac/launchd/sync-knowledge-base.sh` | WatchPaths — CSV change → calendar refresh + KB rebuild + rsync to Ubuntu |
| `sync-knowledge-base-periodic.plist` | same script | Hourly fallback in case WatchPaths misses a CSV update |
| `sync-notes-audio.plist` | `mac/launchd/sync-notes-audio.sh` | Every 5 min — pull `My Notes Audio/*.m4a` to Ubuntu via /tmp |
| `test-pipeline.plist` | `~/.local/bin/test-pipeline.sh` | Daily 06:00 — pytest smoke run |
| `weekly-review.plist` | `~/.local/bin/weekly-review.sh` | Mondays 07:00 — `query_graph.py review --weeks 2` digest |
