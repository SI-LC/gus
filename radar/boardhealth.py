"""When each job board was last read, and what it said when it wasn't.

One scan can only say "this board didn't work this time". The scan records every board it asks here, and a board that fails
keeps the day it last answered, so Gus can say "not read for 6 days": the difference between a bad morning and a board
you've stopped hearing from.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from radar.paths import COMPANIES, DATA
from radar.store import locked, read

PATH = DATA / "board_health.json"
HISTORY = DATA / "board_history.json"
UNREAD_DAYS = 2  # a board unread for this many days is named in the run's summary: one bad morning isn't news
HISTORY_DAYS = 90  # how many days of history are kept
WEEK = 7  # today is compared with this many days ago: a few boards unread is normal, more of them each week is not
NAMED = 5  # how many newly unread boards the run's summary names


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


def _history(path, history):
    """The history kept beside the health file it is drawn from."""
    return history or (path.parent / HISTORY.name if path else HISTORY)


def without_board(companies: dict) -> tuple:
    """How many companies Gus follows have no board for it to read, and how many of those the candidate watches."""
    from radar.companies import read_elsewhere
    from radar.matching import load_config
    cfg, rows = load_config(), _watchlist_rows()
    none = [r for n, r in companies.items() if r.get("status") in ("watched", "maybe", "candidate") and not r.get("board")
            and not read_elsewhere(n, r, cfg, rows)]  # a company read by a careers-site search or under another name isn't unread
    return len(none), sum(r.get("status") == "watched" for r in none)


def _watchlist_rows() -> list:
    from radar.companies import watchlist
    try:
        return watchlist()
    except OSError:
        return []


def keep(now: datetime = None, path=None, history=None, companies=None) -> None:
    """After a scan or a run: today's line in data/board_history.json, replacing an earlier one from today. One scan can
    only say what is unread now; the history is what shows whether Gus is missing more boards than it was."""
    health = read(path or PATH)
    if not health:
        return
    every, watched = without_board(read(companies or COMPANIES))
    with locked(_history(path, history)) as days:
        days[str((now or datetime.now(timezone.utc)).date())] = {
            "boards": len(health), "unread": sorted(n for n, r in health.items() if r.get("failed")),
            "no_board": every, "no_board_watched": watched}
        for day in sorted(days)[:-HISTORY_DAYS]:
            del days[day]


def trend(now: datetime = None, path=None, history=None) -> dict:
    """Today against a week ago, or against the first day on record while the history is shorter: `late`, the boards unread
    for UNREAD_DAYS or more; `new`, the ones of those that were being read then; `days`, how far back the comparison goes
    (None with nothing to compare with yet); `no_board_then`, the companies with no board that day."""
    today = (now or datetime.now(timezone.utc)).date()
    days = read(_history(path, history))
    late = sorted(n for n, u in unread(now, path).items() if u["days"] >= UNREAD_DAYS)
    by_then = [d for d in sorted(days) if d <= str(today - timedelta(days=WEEK))]
    then = by_then[-1] if by_then else min(days, default=None)
    if not then or then >= str(today):
        return {"late": late, "new": [], "days": None, "no_board_then": None}
    return {"late": late, "new": [n for n in late if n not in days[then].get("unread", [])],
            "days": min(WEEK, (today - datetime.fromisoformat(then).date()).days), "no_board_then": days[then].get("no_board")}


def note(path=None, history=None) -> str:
    """For the end of a run: "" when every board has been read lately, or how many haven't and whether any of them is new
    this week. Before there is a week to compare with, the longest wait."""
    late = {n: u["days"] for n, u in unread(path=path).items() if u["days"] >= UNREAD_DAYS}
    if not late:
        return ""
    count = f"{len(late)} job board{'' if len(late) == 1 else 's'} not read for {UNREAD_DAYS} days or more"
    t = trend(path=path, history=history)
    if t["days"] is None:
        worst = max(late, key=late.get)
        return f"{count} ({worst}: {late[worst]} days)"
    span = "week" if t["days"] == WEEK else "day" if t["days"] == 1 else f"{t['days']} days"
    if not t["new"]:
        return f"{count}, none of them new in the last {span}"
    more = len(t["new"]) - NAMED
    return f"{count}, {len(t['new'])} of them new in the last {span}: {', '.join(t['new'][:NAMED])}" + (f" and {more} more" if more > 0 else "")
