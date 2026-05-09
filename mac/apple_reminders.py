"""Thin Apple Reminders client used by the pipeline.

Speaks to Reminders.app via JXA (JavaScript for Automation) scripts run through
``osascript``. The JXA fragments below are ports of the apple-reminders-mcp
project's scripts, inlined here so the pipeline is self-contained — no
dependency on the MCP install path. Only the operations actually needed by
``query_graph.py focus --push`` are exposed.

First run will trigger a macOS Automation permission prompt for whichever
process is invoking ``python3`` (Terminal, VS Code, launchd, etc.). Granting
Reminders access to that parent is required for any of these calls to succeed.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Optional


_JXA_LIST_LISTS = """
function run() {
    try {
        const Reminders = Application('Reminders');
        return JSON.stringify(Reminders.lists().map(function (l) {
            return { id: l.id(), name: l.name() };
        }));
    } catch (e) { return JSON.stringify({ error: e.message }); }
}
"""

_JXA_CREATE_LIST = """
function run(argv) {
    try {
        const name = argv[0];
        if (!name) return JSON.stringify({ error: 'name is required' });
        const Reminders = Application('Reminders');
        if (Reminders.lists.name().indexOf(name) >= 0) {
            return JSON.stringify({ id: null, name: name, created: false });
        }
        const list = Reminders.List({ name: name });
        Reminders.lists.push(list);
        return JSON.stringify({ id: list.id(), name: list.name(), created: true });
    } catch (e) { return JSON.stringify({ error: e.message }); }
}
"""

_JXA_LIST_REMINDERS = """
function run(argv) {
    try {
        const Reminders = Application('Reminders');
        const listName = argv[0];
        const includeCompleted = argv[1] === 'true';
        if (!listName) return JSON.stringify({ error: 'list_name is required' });
        const collection = Reminders.lists.byName(listName).reminders;
        const ids = collection.id();
        const names = collection.name();
        const bodies = collection.body();
        const completed = collection.completed();
        const out = [];
        for (let i = 0; i < ids.length; i++) {
            if (!includeCompleted && completed[i]) continue;
            out.push({ id: ids[i], name: names[i], body: bodies[i] || null, completed: completed[i] });
        }
        return JSON.stringify(out);
    } catch (e) { return JSON.stringify({ error: e.message }); }
}
"""

_JXA_DELETE_REMINDER = """
function run(argv) {
    try {
        const id = argv[0];
        if (!id) return JSON.stringify({ error: 'id is required' });
        const Reminders = Application('Reminders');
        const reminders = Reminders.reminders.whose({ id: id });
        if (!reminders.length) return JSON.stringify({ deleted: false, id: id });
        Reminders.delete(reminders[0]);
        return JSON.stringify({ deleted: true, id: id });
    } catch (e) { return JSON.stringify({ error: e.message }); }
}
"""

_JXA_DELETE_LIST = """
function run(argv) {
    try {
        const name = argv[0];
        if (!name) return JSON.stringify({ error: 'name is required' });
        const Reminders = Application('Reminders');
        const lists = Reminders.lists.whose({ name: name });
        if (!lists.length) return JSON.stringify({ deleted: false, name: name });
        Reminders.delete(lists[0]);
        return JSON.stringify({ deleted: true, name: name });
    } catch (e) { return JSON.stringify({ error: e.message }); }
}
"""

_JXA_CREATE_REMINDER = """
function run(argv) {
    try {
        const listName = argv[0];
        const title = argv[1];
        const notes = argv[2] || '';
        if (!listName) return JSON.stringify({ error: 'list_name is required' });
        if (!title) return JSON.stringify({ error: 'title is required' });
        const Reminders = Application('Reminders');
        const props = { name: title };
        if (notes) props.body = notes;
        const reminder = Reminders.Reminder(props);
        Reminders.lists.byName(listName).reminders.push(reminder);
        return JSON.stringify({ id: reminder.id(), name: reminder.name(), list: listName });
    } catch (e) { return JSON.stringify({ error: e.message }); }
}
"""


def _run_jxa(script: str, *args: str) -> Any:
    cmd = ["osascript", "-l", "JavaScript", "-", *args]
    result = subprocess.run(cmd, input=script, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"osascript exited {result.returncode}: {result.stderr.strip() or '(no stderr)'}"
        )
    out = result.stdout.strip()
    if not out:
        return None
    parsed = json.loads(out)
    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError(f"Reminders error: {parsed['error']}")
    return parsed


def list_lists() -> list[dict]:
    return _run_jxa(_JXA_LIST_LISTS)


def ensure_list(name: str) -> dict:
    """Create the list if missing; return ``{name, created}`` either way."""
    return _run_jxa(_JXA_CREATE_LIST, name)


def list_reminders(list_name: str, include_completed: bool = False) -> list[dict]:
    return _run_jxa(
        _JXA_LIST_REMINDERS, list_name, "true" if include_completed else "false"
    )


def create_reminder(list_name: str, title: str, notes: Optional[str] = None) -> dict:
    return _run_jxa(_JXA_CREATE_REMINDER, list_name, title, notes or "")


def delete_list(name: str) -> dict:
    return _run_jxa(_JXA_DELETE_LIST, name)


def delete_reminder(reminder_id: str) -> dict:
    return _run_jxa(_JXA_DELETE_REMINDER, reminder_id)
