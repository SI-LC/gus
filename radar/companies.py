"""Company records for the watchlist loop: data/companies.json.

Each company moves through: candidate → watched | maybe | rejected.
The agent (prompts/watchlist.md) does the research and assessment; this module
does the cheap, deterministic parts that run on every scan.

Usage:
    python3 -m radar.companies sync               # every watchlist company has a record
    python3 -m radar.companies mentions           # flag watched companies named in new signals
    python3 -m radar.companies health             # flag watched companies whose board keeps failing
    python3 -m radar.companies due                # list what the agent should (re)assess
    python3 -m radar.companies veto "Spotify"     # remove from watchlist, never re-add

How an agent reads and changes records, several at a time, so a run doesn't search the file or edit it line by line:
    python3 -m radar.companies known "Acme" "Newco"          # does Gus have each one, and as what
    python3 -m radar.companies show "Acme" "Newco"           # their records
    python3 -m radar.companies set '{"Acme": {"fit": 8, "fit_why": "...", "status": "watched"}, "Newco": {"source": "..."}}'
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date, timedelta

from radar import web
from radar.jobs import same_company
from radar.paths import COMPANIES, DATA, SIGNALS, watchlist_path
from radar.store import locked, read

BOARD_ERRORS = DATA / "board_errors.json"
STALE_DAYS = 30
NEIGHBOUR_DAYS = 28
CAREERS_DAYS = 3  # how often an agent reads the careers page of a watched company that has no board
CAREERS_PER_RUN = 8  # companies an agent reads that way per run, the candidate's own choices first
YOURS = ["favourite", "vetoed", "tier_locked", "website_locked", "board_guess_rejected"]  # only the candidate sets these
REMOVED = "removed by the candidate, never re-add"


def load():
    return read(COMPANIES)


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
    signals = read(SIGNALS)
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
                lead.setdefault("company", name)  # the watchlist agent reads new signals by company
                c[name]["reassess"] = True
                flagged += 1
        lead["mention_checked"] = True
    save(c)
    SIGNALS.write_text(json.dumps(signals, indent=2, ensure_ascii=False))
    return flagged


def health():
    """Boards that errored on the last scan, e.g. the company switched ATS."""
    errs = read(BOARD_ERRORS)
    c = load()
    for name, rec in c.items():
        if name in errs:
            rec["board_broken"] = errs[name]
        else:
            rec.pop("board_broken", None)
    save(c)


# One search of a big employer's own careers site covers every company under it: the setting that turns it on, and the names it reads
SITE_SEARCHES = {"google_careers_search": ("google", "deepmind"), "amazon_jobs_search": ("amazon", "aws"),
                 "microsoft_careers_search": ("microsoft",), "apple_jobs_search": ("apple",)}


def read_elsewhere(name: str, rec: dict, cfg: dict = None, rows: list = None) -> str | None:
    """How Gus already reads a company that has no board of its own, or None when nothing does: a careers-site search
    that covers it ("Google DeepMind" under Google's), or a board on the watchlist under another name for the same company."""
    from radar.matching import load_config
    from radar.resolve import named_boards
    cfg = load_config() if cfg is None else cfg
    words = set(re.findall(r"[a-z0-9]+", name.lower()))
    for setting, names in SITE_SEARCHES.items():
        if cfg.get(setting) and words & set(names):
            return f"the {names[0].capitalize()} careers search"
    on_watchlist = {(r["ats"], r["slug"].lower()): r["company"] for r in (watchlist() if rows is None else rows)}
    for ats, slug in named_boards(rec.get("careers_url") or ""):
        if owner := on_watchlist.get((ats, slug.lower())):
            return f"{owner}'s board"
    return None


def unread(c: dict) -> dict:
    """The companies Gus follows that nothing reads: no board of their own, and not read some other way."""
    from radar.matching import load_config
    cfg, rows = load_config(), watchlist()
    return {n: r for n, r in c.items() if r.get("status") in ("watched", "maybe", "candidate") and not r.get("vetoed")
            and not r.get("board") and not read_elsewhere(n, r, cfg, rows)}


def no_board(c: dict) -> list:
    """Watched companies nothing reads: no board, and their careers page not read by an agent lately. The candidate's
    favourites and tier A first, then whoever has waited longest, a few per run so the agent's budget lasts."""
    cutoff = str(date.today() - timedelta(days=CAREERS_DAYS))
    names = [n for n, r in unread(c).items() if r.get("status") == "watched" and (r.get("careers_checked") or "") <= cutoff]
    first = lambda n: (not (c[n].get("favourite") or c[n].get("tier_locked")), c[n].get("tier") or "Z", c[n].get("careers_checked") or "")
    return sorted(names, key=first)[:CAREERS_PER_RUN]


def due():
    c = load()
    cutoff = str(date.today() - timedelta(days=STALE_DAYS))
    today = str(date.today())
    out = {"candidates": [], "reassess": [], "stale": [], "broken_boards": [], "recheck_rejected": [],
           "favourites": [n for n, r in c.items() if r.get("favourite") and not r.get("vetoed")
                          and (r.get("neighbours_checked") or "") < str(date.today() - timedelta(days=NEIGHBOUR_DAYS))]}
    out["no_board"] = no_board(c)
    for n, r in c.items():
        s = r.get("status", "candidate")
        if s == "candidate":
            out["candidates"].append(n)
        elif s in ("watched", "maybe"):
            if r.get("reassess"):
                out["reassess"].append(n)
            elif (r.get("assessed") or r.get("updated") or "") < cutoff:
                out["stale"].append(n)
            if r.get("board_broken") and not web.said_slow_down(r["board_broken"]):  # told to slow down: asking again makes it worse
                out["broken_boards"].append(n)
        elif s == "rejected" and not r.get("vetoed") and (r.get("recheck_after") or "9999") <= today:
            out["recheck_rejected"].append(n)
    return out


def find(name, c: dict) -> str | None:
    """A company as Gus spells it, however it was typed."""
    name = str(name or "").strip()
    return next((n for n in c if n.lower() == name.lower()), None) or \
        next((n for n in c if same_company(n) and same_company(n) == same_company(name)), None)


def known(names: list[str]) -> dict:
    """For each name: how Gus has it ("Acme: watched, tier A"), or None when it's new."""
    c, out = load(), {}
    for name in names:
        real = find(name, c)
        rec = c.get(real) or {}
        out[name] = None if not real else f"{real}: {REMOVED}" if rec.get("vetoed") else \
            f"{real}: {rec.get('status', 'candidate')}" + (f", tier {rec['tier']}" if rec.get("tier") else "")
    return out


def show(names: list[str]) -> dict:
    c = load()
    return {name: c.get(find(name, c)) for name in names}


def set_many(updates: dict) -> dict:
    """Add companies and change fields on several records at once. A value of null clears a field. Answers {name: "added",
    "saved" or why not}. The candidate's own choices stay as they are: a removed company is never touched, a favourite
    or a locked tier keeps its status and tier, and the fields in YOURS can't be set."""
    from radar.schema import check_record
    out = {}
    with locked(COMPANIES) as c:
        for name, fields in (updates if isinstance(updates, dict) else {}).items():
            if not isinstance(fields, dict) or not str(name).strip():
                out[name] = "not saved: give each company an object of fields"
                continue
            real = find(name, c)
            rec = dict(c.get(real) or {})
            if rec.get("vetoed"):
                out[name] = f"not saved: {REMOVED}"
                continue
            kept = [k for k in fields if k in YOURS]
            if rec.get("tier_locked") and "tier" in fields and fields["tier"] != rec.get("tier"):
                kept.append("tier")
            if (rec.get("favourite") or rec.get("tier_locked")) and fields.get("status", "watched") != "watched":
                kept.append("status")
            before = rec.get("fit")
            for k, v in fields.items():
                if k in kept:
                    continue
                if v is None:
                    rec.pop(k, None)
                else:
                    rec[k] = v
            rec.setdefault("status", "candidate")
            if before is not None and rec.get("fit") != before and "previous_fit" not in fields:
                rec["previous_fit"] = before
            if errs := check_record("companies", real or str(name), rec, {}, {}):
                out[name] = "not saved: " + "; ".join(errs)
                continue
            c[real or str(name).strip()] = rec
            out[name] = ("saved" if real else "added") + (f"; kept {', '.join(kept)} as the candidate set {'it' if len(kept) == 1 else 'them'}" if kept else "")
    return out


def veto(name: str):
    c = load()
    rows = watchlist()
    keep = [r for r in rows if r["company"].lower() != name.lower()]
    with watchlist_path().open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["company", "ats", "slug", "sector"])
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
    elif cmd == "known":
        print(json.dumps(known(sys.argv[2:]), ensure_ascii=False))
    elif cmd == "show":
        print(json.dumps(show(sys.argv[2:]), ensure_ascii=False))
    elif cmd == "set":
        try:
            updates = json.loads(sys.argv[2])
        except (IndexError, ValueError):
            sys.exit("set takes one JSON object: '{\"Company\": {\"field\": \"value\"}}'")
        print(json.dumps(set_many(updates), ensure_ascii=False))


if __name__ == "__main__":
    main()
