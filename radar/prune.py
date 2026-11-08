"""Keep the data files small without losing anything you acted on.

    python3 -m radar.prune            # show what would go
    python3 -m radar.prune --apply    # do it (the daily run does this after `events sync`)

After `prune_after_days` (a setting, 60 by default):
- a closed or skipped role you never touched loses its saved advert text, the biggest thing in jobs.json; the record
  stays, so the role can't come back as new and its history is kept;
- an ignored or processed signal that became nothing moves to data/archive/signals.json;
- a closed event no move points at moves to data/archive/events.json.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from email.utils import parsedate_to_datetime

from radar.accounts import MOVES
from radar.events import EVENTS, SIGNALS, parse_date
from radar.jobs import is_live
from radar.scan import DATA, JOBS, load_config
from radar.store import locked, read

ARCHIVE = DATA / "archive"
UNTOUCHED = ("new", "skipped", "closed")  # a role you prepared or applied for keeps its text


def when(s):
    """A date from any of the shapes the files hold, including the RFC 2822 dates feeds give."""
    d = parse_date(s)
    if d is None and s:
        try:
            d = parsedate_to_datetime(str(s)).date()
        except (TypeError, ValueError):
            d = None
    return d


def role_closed_on(j: dict):
    history = j.get("history") or []
    return when(j.get("ad_closed")) or (when(history[-1].get("at")) if history else None) or when(j.get("last_seen"))


def prunable_roles(store: dict, cutoff: date) -> list[str]:
    out = []
    for jid, j in store.items():
        if is_live(j) or j.get("stage") not in UNTOUCHED or j.get("note") or j.get("folder") or not j.get("description"):
            continue
        closed = role_closed_on(j)
        if closed and closed < cutoff:
            out.append(jid)
    return out


def prunable_signals(signals: dict, cutoff: date) -> list[str]:
    return [sid for sid, s in signals.items()
            if s.get("status") in ("ignored", "processed") and not s.get("event_id") and not s.get("job_id")
            and (when(s.get("first_seen")) or when(s.get("date")) or cutoff) < cutoff]


def prunable_events(events: dict, moves: dict, cutoff: date) -> list[str]:
    used = {m.get("event_id") for m in moves.values()}
    return [eid for eid, e in events.items() if e.get("closed") and eid not in used and (when(e.get("closed")) or cutoff) < cutoff]


def archive(name: str, records: dict) -> None:
    ARCHIVE.mkdir(exist_ok=True)
    with locked(ARCHIVE / f"{name}.json") as kept:
        kept.update(records)


def prune(apply: bool = False, today: date | None = None, cfg: dict | None = None) -> dict:
    cfg = load_config() if cfg is None else cfg
    today = today or date.today()
    cutoff = today - timedelta(days=int(cfg.get("prune_after_days", 60)))
    out = {"roles": 0, "signals": 0, "events": 0, "cutoff": str(cutoff)}
    moves = read(MOVES)
    if not apply:  # a look, with no file rewritten
        out["roles"] = len(prunable_roles(read(JOBS), cutoff))
        out["signals"] = len(prunable_signals(read(SIGNALS), cutoff))
        out["events"] = len(prunable_events(read(EVENTS), moves, cutoff))
        return out
    with locked(JOBS) as store:
        ids = prunable_roles(store, cutoff)
        for jid in ids:
            store[jid]["description"] = ""
            store[jid]["pruned"] = str(today)
        out["roles"] = len(ids)
    with locked(SIGNALS) as signals:
        ids = prunable_signals(signals, cutoff)
        if ids:
            archive("signals", {sid: signals.pop(sid) for sid in ids})
        out["signals"] = len(ids)
    with locked(EVENTS) as events:
        ids = prunable_events(events, moves, cutoff)
        if ids:
            archive("events", {eid: events.pop(eid) for eid in ids})
        out["events"] = len(ids)
    return out


def main():
    apply = "--apply" in sys.argv
    out = prune(apply)
    verb = "pruned" if apply else "would prune"
    print(f"{verb} {out['roles']} roles' text, {out['signals']} signals, {out['events']} events older than {out['cutoff']}")
    if not apply:
        print("Nothing changed. Run again with --apply.", file=sys.stderr)


if __name__ == "__main__":
    main()
