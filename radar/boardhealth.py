"""When each job board was last read, and what it said when it wasn't.

One scan can only say "this board didn't work this time". The scan records every board it asks here, and a board that fails
keeps the day it last answered, so Gus can say "not read for 6 days": the difference between a bad morning and a board
you've stopped hearing from.
"""
from __future__ import annotations

from datetime import datetime, timezone

from radar.paths import DATA
from radar.store import locked, read

PATH = DATA / "board_health.json"
UNREAD_DAYS = 2  # a board unread for this many days is named in the run's summary: one bad morning isn't news


def record(asked: dict, errors: dict, now: str, path=None, partial: bool = False) -> None:
    """After a scan. asked is {company: "ats:slug"} for every board it tried, errors is {company: what went wrong}.
    A board that has left the watchlist is forgotten, and one that has moved starts afresh. partial: only some boards were
    asked (the ones held back earlier in the day), so the rest are left as they were."""
    with locked(path or PATH) as health:
        for name in [] if partial else [n for n in health if n not in asked]:
            del health[name]
        for name, board in asked.items():
            rec = health.get(name) or {}
            if name not in errors:
                health[name] = {"board": board, "read": now}
                continue
            if rec.get("board") != board:
                rec = {"board": board}
            rec["failed"] = errors[name]
            rec.setdefault("since", now)  # when this run of failures began
            health[name] = rec


def unread(now: datetime = None, path=None) -> dict:
    """{company: {"days", "failed"}} for every board that failed on the last scan. days counts from the last time it was
    read, or from its first failure when Gus has never read it."""
    today = (now or datetime.now(timezone.utc)).date()
    out = {}
    for name, rec in read(path or PATH).items():
        if not rec.get("failed"):
            continue
        try:
            last = datetime.fromisoformat(rec.get("read") or rec["since"]).date()
        except (KeyError, ValueError):
            last = today
        out[name] = {"days": max(0, (today - last).days), "failed": rec["failed"]}
    return out


def note(path=None) -> str:
    """For the end of a run: "" when every board has been read lately, or which haven't and the longest wait."""
    late = {n: u["days"] for n, u in unread(path=path).items() if u["days"] >= UNREAD_DAYS}
    if not late:
        return ""
    worst = max(late, key=late.get)
    count = f"{len(late)} job board{'' if len(late) == 1 else 's'} not read for {UNREAD_DAYS} days or more"
    return f"{count} ({worst}: {late[worst]} days)"
