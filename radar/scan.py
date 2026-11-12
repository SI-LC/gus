"""One scan: read every board on the watchlist, keep target roles in the target locations, record anything not seen
before, and mark roles closed once they've gone from a board that answered.

Usage:
    python3 -m radar.scan            # scan everything, print new roles
    python3 -m radar.scan --json     # same, as JSON
"""
from __future__ import annotations

import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from radar import boards, matching
from radar import jobs as J
from radar.paths import DATA, JOBS, watchlist_path
from radar.store import locked

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


def fetch_all():
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

    answered = set()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for (_, ats, slug, _), found, err in pool.map(run, tasks):
            jobs += found
            if err:
                errors.append(err)
            elif ats in boards.PREFIX and slug not in boards.cut_short:
                answered.add(f"{boards.PREFIX[ats]}:{slug}")
    _boards_answered.clear()
    _boards_answered.update(answered)
    boards.save_looked_up(answered)
    (DATA / "board_errors.json").write_text(json.dumps(dict(e for e in errors if isinstance(e, tuple))))
    errors = [e if isinstance(e, str) else f"{e[0]} ({e[1]})" for e in errors]
    if matching.load_config().get("source_hn", True):
        try:
            jobs += [dict(j, _hn=True) for j in boards.hn_who_is_hiring()]
        except Exception as e:
            errors.append(f"HN: {e}")
    return jobs, errors


def main():
    cfg = matching.load_config()
    DATA.mkdir(exist_ok=True)
    jobs, errors = fetch_all()  # slow network part, outside the lock
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new = []
    live = {j["id"] for j in jobs} | boards.listed_ids
    with locked(JOBS) as store:
      mark_closed(store, _boards_answered, live, now)
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
      total = len(store)

    if "--json" in sys.argv:
        print(json.dumps({"new": new, "errors": errors}, indent=2, ensure_ascii=False))
        return
    print(f"{len(new)} new of {total} tracked roles")
    for j in new:
        print(f"  • {j['company']}: {j['title'][:80]}  [{j['location'][:40]}]\n    {j['url']}")
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
