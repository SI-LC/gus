"""One scan: read every board on the watchlist, keep target roles in the target locations, record anything not seen
before, and mark roles closed once they've gone from a board that answered.

Usage:
    python3 -m radar.scan            # scan everything, print new roles
    python3 -m radar.scan --json     # same, as JSON
    python3 -m radar.scan --held     # only the boards a vendor held back on the last scan ("slow down"), now it may be asked
    python3 -m radar.scan --new      # only the boards no scan has asked yet: the ones added since the last scan

Each scan adds to data/scan.log when it ran, which boards it asked, how long it took and what each unread board said,
and each request it made to a slowly asked job site that turned one of them away.
"""
from __future__ import annotations

import contextlib
import csv
import itertools
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from radar import boardhealth, boards, doing, matching, web
from radar import jobs as J
from radar.paths import DATA, JOBS, watchlist_path
from radar.store import locked, read

LOG_LINES = 3000  # how much of data/scan.log is kept: a few weeks of scans
MISSES_TO_CLOSE = 2  # scans in a row a role must be missing before it counts as closed
_boards_answered: set = set()


def mark_closed(store: dict, answered: set, live: set, now: str) -> list:
    """Roles gone from a board that answered, two scans running, are marked closed; if one returns, it's reopened.
    A role you added by hand is yours to close."""
    closed = []
    for jid, j in store.items():
        if j.get("source") == "manual":  # you added it by hand, a recruiter's role say: no board's silence closes it
            continue
        ids = [jid] + list(j.get("also") or {})
        if not any(":".join(i.split(":")[:2]) in answered for i in ids):
            continue
        if any(i in live for i in ids):
            j.pop("missed", None)
            j.pop("ad_closed", None)
        elif not j.get("ad_closed"):
            j["missed"] = j.get("missed", 0) + 1
            if j["missed"] >= MISSES_TO_CLOSE:
                j["ad_closed"] = now
                closed.append(j)
    return closed


def waiting() -> set:
    """Companies whose board wasn't read on the last scan because its vendor said slow down: waiting cures those, so they're
    read again the same day. A board that broke or moved isn't one of them."""
    return {name for name, failure in read(DATA / "board_errors.json").items() if web.held_back(failure)}


def unasked() -> set:
    """Companies whose board no scan has asked yet: added to the watchlist since the last scan, or moved to another board.
    A run's later scans read only these. The morning's scan has asked every other board, and asking a vendor for all its
    boards three times in one run is how it comes to say slow down."""
    health = read(DATA / "board_health.json")
    return {r["company"] for r in csv.DictReader(watchlist_path().open())
            if r["ats"] in boards.SOURCES and (health.get(r["company"]) or {}).get("board") != f"{r['ats']}:{r['slug']}"}


def log(*lines: str) -> None:
    """Add to data/scan.log, keeping the last LOG_LINES: what the scheduler's scans said, which nothing else keeps."""
    path = DATA / "scan.log"
    kept = (path.read_text().splitlines() if path.exists() else []) + list(lines)
    path.write_text("\n".join(kept[-LOG_LINES:]) + "\n")


def fetch_all(only: set = None):
    """Every board, or with `only` just those companies' boards: the rest keep what the last scan recorded for them."""
    rows = list(csv.DictReader(watchlist_path().open()))
    # `only`: a company that shares a parent's board (Base44 on Wix's) keeps just the titles matching this pattern
    tasks = [(r["company"], r["ats"], r["slug"], r.get("only") or None) for r in rows if r["ats"] in boards.SOURCES]
    if matching.load_config().get("google_careers_search"):
        tasks.append(("Google", "googlecareers", "search", None))
    if matching.load_config().get("amazon_jobs_search"):
        tasks.append(("Amazon", "amazonjobs", "amazon", None))
    if matching.load_config().get("microsoft_careers_search"):
        tasks.append(("Microsoft", "microsoftcareers", "microsoft", None))
    if matching.load_config().get("apple_jobs_search"):
        tasks.append(("Apple", "applejobs", "apple", None))
    if matching.load_config().get("source_mindtheproduct"):
        tasks.append(("Mind the Product", "mindtheproduct", "jobs", None))
    tasks += [(host, "getro", host, None) for host in boards.getro_hosts(matching.load_config().get("getro_boards"))]
    if only is not None:
        tasks = [t for t in tasks if t[0] in only]
    errors, jobs = [], []

    def run(t):
        company, ats, slug, only = t
        try:
            found = boards.SOURCES[ats](company, slug)
            if only:
                found = [j for j in found if re.search(only, j["title"], re.I)]
            return t, found, None
        except Exception as e:  # one dead board shouldn't stop the scan
            return t, [], (company, f"{ats}:{slug}: {e}")

    # A vendor Gus asks slowly (Workday always, others for a week after saying "slow down") has its boards read one after
    # another in a slot of its own: waiting their turn in the six, they would hold up every other board for many minutes.
    slow = {}
    for t in tasks:
        if (who := boards.VENDORS.get(t[1])) and web.unhurried(who):
            slow.setdefault(who, []).append(t)
    quick = [t for t in tasks if t not in {s for lane in slow.values() for s in lane}]
    answered = set()
    with ThreadPoolExecutor(max_workers=6) as pool, contextlib.ExitStack() as lanes:
        waiting_on = []
        for lane in slow.values():  # one worker for each slow vendor, so its boards finish in turn and each is reported as it does
            one_at_a_time = lanes.enter_context(ThreadPoolExecutor(max_workers=1))
            waiting_on += [one_at_a_time.submit(run, t) for t in lane]
        results = itertools.chain(pool.map(run, quick), (f.result() for f in as_completed(waiting_on)))
        for n, ((company, ats, slug, _), found, err) in enumerate(results, 1):
            left = len(tasks) - n
            rest = f". The other {left} {'is on a job site' if left == 1 else 'are on job sites'} Gus asks slowly"
            doing.say(f"Reading job boards: {n} of {len(tasks)} done ({company}), {len(jobs) + len(found)} ads so far" + (rest if len(quick) <= n < len(tasks) else ""))
            jobs += found
            if err:
                errors.append(err)
            elif ats in boards.PREFIX and slug not in boards.cut_short:
                answered.add(f"{boards.PREFIX[ats]}:{slug}")
    _boards_answered.clear()
    _boards_answered.update(answered)
    boards.save_looked_up(answered)
    failed = dict(e for e in errors if isinstance(e, tuple))
    asked = {t[0]: f"{t[1]}:{t[2]}" for t in tasks}
    if only is not None:  # boards not asked this time keep the failure they had
        failed = {**{n: f for n, f in read(DATA / "board_errors.json").items() if n not in asked}, **failed}
    (DATA / "board_errors.json").write_text(json.dumps(failed))
    boardhealth.record(asked, failed, datetime.now(timezone.utc).isoformat(timespec="seconds"), DATA / "board_health.json",
                       partial=only is not None)
    boardhealth.keep(path=DATA / "board_health.json", companies=DATA / "companies.json")
    errors = [e if isinstance(e, str) else f"{e[0]} ({e[1]})" for e in errors]
    if only is None and matching.load_config().get("source_hn", True):
        doing.say("Reading Hacker News: Who is hiring")
        try:
            jobs += [dict(j, _hn=True) for j in boards.hn_who_is_hiring()]
        except Exception as e:
            errors.append(f"HN: {e}")
    return jobs, errors


def main():
    cfg = matching.load_config()
    DATA.mkdir(exist_ok=True)
    held = waiting() if "--held" in sys.argv else None
    fresh = unasked() if "--new" in sys.argv and held is None else None
    if held is not None and not held:
        print("No boards are waiting to be read")
        return
    if fresh is not None and not fresh:
        print("No new boards to read")
        return
    web.remember_robots(DATA / "robots.json")
    began = time.monotonic()
    only = held or fresh
    jobs, errors = fetch_all(only) if only else fetch_all()  # slow network part, outside the lock
    web.keep_robots(DATA / "robots.json")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new = []
    live = {j["id"] for j in jobs} | boards.listed_ids
    with locked(JOBS) as store:
      mark_closed(store, _boards_answered, live, now)
      J.settle_sources(store)  # older records: a job site's role already found on the board
      J.rename_skip_reasons(store)  # older records: a reason the list no longer has
      alias = J.aliases(store)
      for j in jobs:
        hn = j.pop("_hn", False)
        if not matching.matches(j, cfg, hn):
            continue
        if j["id"] in store:
            store[j["id"]]["last_seen"] = now
            if j.get("ref"):
                store[j["id"]].setdefault("ref", j["ref"])
            continue
        if (have := J.find_match(store, j, alias)) is not None:  # the same ad, found before through another source
            J.absorb(store[have], {**j, "source": "hn" if hn else "board"}, now)
            alias[j["id"]] = have
            continue
        j.update(first_seen=now, last_seen=now, stage="new", score=None, why=None, source="hn" if hn else "board")
        store[j["id"]] = j
        new.append(j)
      J.mark_same_adverts(store)  # roles that share one advert say so; they're never merged for it
      total = len(store)

    asked = len(held or fresh or read(DATA / "board_health.json"))
    not_read = [e for e in errors if not e.startswith("HN: ")]
    what = "boards held back" if held else "new boards" if fresh else "every board"
    log(f"{now} {what}: {asked - len(not_read)} of {asked} read in {time.monotonic() - began:.0f}s, {len(new)} new role{'' if len(new) == 1 else 's'}",
        *(f"  {e}" for e in errors), *(f"    {line}" for line in web.asked_lines()))
    if "--json" in sys.argv:
        print(json.dumps({"new": new, "errors": errors}, indent=2, ensure_ascii=False))
        return
    if fresh:
        print(f"{len(fresh)} new board{'' if len(fresh) == 1 else 's'} read: {len(new)} new role{'' if len(new) == 1 else 's'}")
    elif held:
        left = len(waiting() & held)
        print(f"{len(held) - left} of {len(held)} boards that were told to wait are now read"
              + (f", {left} still waiting" if left else "") + f": {len(new)} new role{'' if len(new) == 1 else 's'}")
    else:
        print(f"{len(new)} new of {total} tracked roles")
    for j in new:
        print(f"  • {j['company']}: {j['title'][:80]}  [{j['location'][:40]}]\n    {j['url']}")
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
