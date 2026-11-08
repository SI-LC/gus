"""Company records for the watchlist loop: data/companies.json.

Each company moves through: candidate → watched | maybe | rejected.
The agent (prompts/watchlist.md) does the research and assessment; this module
does the cheap, deterministic parts that run on every scan.

Usage:
    python3 -m radar.companies sync               # every watchlist company has a record
    python3 -m radar.companies mentions           # flag watched companies named in new signals
    python3 -m radar.companies due                # list what the agent should (re)assess
    python3 -m radar.companies veto "Spotify"     # remove from watchlist, never re-add
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date, timedelta

from radar.scan import DATA, WATCHLIST, watchlist_path

COMPANIES = DATA / "companies.json"
SIGNALS = DATA / "signals.json"
BOARD_ERRORS = DATA / "board_errors.json"
STALE_DAYS = 30
NEIGHBOUR_DAYS = 28


def load():
    return json.loads(COMPANIES.read_text()) if COMPANIES.exists() else {}


def save(c):
    tmp = COMPANIES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(c, indent=2, ensure_ascii=False))
    tmp.replace(COMPANIES)


def watchlist():
    return list(csv.DictReader(watchlist_path().open()))


def sync():
    c = load()
    for row in watchlist():
        rec = c.setdefault(row["company"], {})
        rec.setdefault("status", "watched")
        rec["board"] = f"{row['ats']}:{row['slug']}"
    for rec in c.values():  # older records used a second name for the same field
        old = rec.pop("why_it_fits", None)
        if old and not rec.get("fit_why"):
            rec["fit_why"] = old
    save(c)
    return c


def mentions():
    """Free, runs every scan: a watched company named in a new signal gets flagged."""
    c = load()
    signals = json.loads(SIGNALS.read_text()) if SIGNALS.exists() else {}
    watched = [n for n, r in c.items() if r.get("status") in ("watched", "maybe")]
    # case-sensitive whole word, so "Wise" the company doesn't match "otherwise"
    pats = {n: re.compile(rf"(?<![\w-]){re.escape(n)}(?![\w-])") for n in watched}
    flagged = 0
    for lead in signals.values():
        if lead.get("mention_checked"):
            continue
        text = f"{lead.get('title', '')} {lead.get('text', '')}"
        for name, pat in pats.items():
            if pat.search(text):
                sig = c[name].setdefault("pending_signals", [])
                if lead.get("url") not in [s["url"] for s in sig]:
                    sig.append({"title": (lead.get("title") or lead.get("text", ""))[:160],
                                "url": lead.get("url"), "source": lead.get("source")})
                    c[name]["reassess"] = True
                    flagged += 1
        lead["mention_checked"] = True
    save(c)
    SIGNALS.write_text(json.dumps(signals, indent=2, ensure_ascii=False))
    return flagged


def health():
    """Boards that errored on the last scan, e.g. the company switched ATS."""
    errs = json.loads(BOARD_ERRORS.read_text()) if BOARD_ERRORS.exists() else {}
    c = load()
    for name, rec in c.items():
        if name in errs:
            rec["board_broken"] = errs[name]
        else:
            rec.pop("board_broken", None)
    save(c)


def due():
    c = load()
    cutoff = str(date.today() - timedelta(days=STALE_DAYS))
    today = str(date.today())
    out = {"candidates": [], "reassess": [], "stale": [], "broken_boards": [], "recheck_rejected": [],
           "favourites": [n for n, r in c.items() if r.get("favourite") and not r.get("vetoed")
                          and (r.get("neighbours_checked") or "") < str(date.today() - timedelta(days=NEIGHBOUR_DAYS))]}
    for n, r in c.items():
        s = r.get("status", "candidate")
        if s == "candidate":
            out["candidates"].append(n)
        elif s in ("watched", "maybe"):
            if r.get("reassess"):
                out["reassess"].append(n)
            elif (r.get("assessed") or r.get("updated") or "") < cutoff:
                out["stale"].append(n)
            if r.get("board_broken"):
                out["broken_boards"].append(n)
        elif s == "rejected" and not r.get("vetoed") and (r.get("recheck_after") or "9999") <= today:
            out["recheck_rejected"].append(n)
    return out


def veto(name: str):
    c = load()
    rows = watchlist()
    keep = [r for r in rows if r["company"].lower() != name.lower()]
    with WATCHLIST.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["company", "ats", "slug", "sector"])
        w.writeheader()
        w.writerows(keep)
    match = next((k for k in c if k.lower() == name.lower()), name)
    c.setdefault(match, {}).update(status="rejected", vetoed=True, fit_why="Removed by you", assessed=str(date.today()))
    save(c)
    print(f"Removed {match} from the watchlist ({len(rows) - len(keep)} board). It won't be re-added.")


def accept_board(name: str) -> bool:
    """You've looked at a board found by name and it's theirs: watch it."""
    c = load()
    guess = (c.get(name) or {}).get("board_guess")
    if not guess:
        return False
    ats, slug = guess["board"].split(":", 1)
    from radar.resolve import add_board
    add_board(name, ats, slug)
    c[name].pop("board_guess", None)
    c[name]["board"] = guess["board"]
    save(c)
    sync()
    return True


def reject_board(name: str) -> bool:
    """It's another company's board: never offer this one again."""
    c = load()
    guess = (c.get(name) or {}).pop("board_guess", None)
    if not guess:
        return False
    rejected = c[name].setdefault("board_guess_rejected", [])
    if guess["board"] not in rejected:
        rejected.append(guess["board"])
    save(c)
    return True


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "due"
    if cmd == "sync":
        print(f"{len(sync())} company records")
    elif cmd == "mentions":
        print(f"{mentions()} new signals on watched companies")
    elif cmd == "health":
        health()
    elif cmd == "due":
        sync()
        health()
        print(json.dumps(due(), indent=2))
    elif cmd == "veto":
        veto(" ".join(sys.argv[2:]))


if __name__ == "__main__":
    main()
