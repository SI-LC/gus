"""The log of changes to your roles: data/changes.jsonl, one change to one field per line, newest last, never edited.

Who made each change and why, with the value before and after, so an overwritten note is never lost and "what changed
since yesterday?" is one read. Only the decisions on a role are logged (its stage, the reason for skipping it, its note,
its folder and a link you or Claude changed), not what its ad says or a link Gus found for it. Lines are appended beside jobs.json while it is locked, by the code that changes it:
`server.update_job` (you in the cockpit, Claude over MCP) and triage's automatic skip. See docs/objects.md.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from pathlib import Path

from radar.paths import JOBS
from radar.schema import CHANGE_ACTORS, CHANGE_FIELDS

ACTORS, FIELDS = CHANGE_ACTORS, CHANGE_FIELDS  # who may make a change, and the fields the log watches



def log_of(jobs: Path = JOBS) -> Path:
    """The log beside the jobs store it records: data/changes.jsonl, or a test's own folder when it uses its own store."""
    return jobs.with_name("changes.jsonl")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decisions(role: dict) -> dict:
    """The fields the log watches, as they are on a role now."""
    return {f: role.get(f) for f in FIELDS}


def diff(role_id: str, before: dict, after: dict, actor: str, reason: str | None = None, at: str | None = None) -> list[dict]:
    """One change for each watched field that differs. Missing and empty are the same: no change."""
    at = at or now()
    stamp = at.replace("-", "").replace(":", "").replace("+0000", "Z")[:15]
    out = []
    for f in FIELDS:
        old, new = before.get(f) or None, after.get(f) or None
        if old != new:
            c = {"id": f"chg_{stamp}_{secrets.token_hex(2)}", "at": at, "role": role_id, "field": f, "old": old, "new": new, "actor": actor}
            if reason:
                c["reason"] = reason
            out.append(c)
    return out


def append(changes: list[dict], jobs: Path = JOBS) -> None:
    """Add these to the log of this jobs store. Call it holding that store's lock, so lines arrive in the order made."""
    if not changes:
        return
    path = log_of(jobs)
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in changes))


def all_changes(jobs: Path = JOBS) -> list[dict]:
    """Every change, oldest first. A line that isn't whole (a write cut off by a power cut) is left out."""
    try:
        lines = log_of(jobs).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if isinstance(c, dict):
            out.append(c)
    return out


def when(text) -> datetime | None:
    """A time or a day (2026-11-14) as given, in UTC; None when it isn't one."""
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def find(since=None, until=None, actor=None, role_id=None, jobs: Path = JOBS) -> list[dict]:
    """The changes in this window, newest first. `since` left out is the last 24 hours."""
    start = when(since) if since else datetime.now(timezone.utc) - timedelta(days=1)
    end = when(until) if until else None
    out = []
    for c in all_changes(jobs):
        t = when(c.get("at"))
        if not t or t < start or (end and t >= end) or (actor and c.get("actor") != actor) or (role_id and c.get("role") != role_id):
            continue
        out.append((t, c))
    out.reverse()  # newest first, and among changes made in the same second, the last written first
    return [c for _t, c in sorted(out, key=lambda x: x[0], reverse=True)]


def named(changes: list[dict], roles: dict) -> list[dict]:
    """The same changes, each carrying its role's company and title, so they read without opening the role."""
    return [{**c, "company": (roles.get(c.get("role")) or {}).get("company"), "title": (roles.get(c.get("role")) or {}).get("title")}
            for c in changes]
