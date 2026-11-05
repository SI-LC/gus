"""Point roles from job search sources at the employer's own ad, so you don't land on a job site's login.

    python3 -m radar.links     # check roles in your inbox that haven't been checked yet (run after each sources job)

Public job boards are free to read, so this uses no AI and none of your Adzuna or JSearch allowance. A link only changes
when the same title is found on the company's board. Otherwise the role keeps its link and the cockpit offers a web search.
"""
from __future__ import annotations

import csv
import json
from datetime import date

from radar.resolve import resolve
from radar.scan import JOBS, SOURCES, watchlist_path
from radar.sources import LABEL, same_company, same_title
from radar.store import locked

MIN_SCORE = 5  # the same line as the Role Inbox: anything you might open
PER_RUN = 40  # each company tries several boards and names, so keep a run polite


def find(company, title, boards):
    """The employer's own ad for this title, or None. `boards` remembers each company's board for this run."""
    name = same_company(company)
    if name not in boards:
        boards[name] = resolve(name) if name else None
    if not boards[name]:
        return None
    ats, slug, _ = boards[name]
    wanted = same_title(title)
    for row in SOURCES[ats](company, slug):
        if same_title(row["title"]) == wanted and row.get("url"):
            return {"url": row["url"], "ats": ats, "slug": slug}
    return None


def waiting(store) -> list:
    return [j for j in store.values() if j.get("source") in LABEL and (j.get("score") or 0) >= MIN_SCORE
            and j.get("stage") != "skipped" and not j.get("ad_closed") and not j.get("link_checked")][:PER_RUN]


def watched() -> set:
    return {same_company(r["company"]) for r in csv.DictReader(watchlist_path().open())}


def watch(job) -> bool:
    """Add the role's company to the watchlist, using the board already confirmed by finding this role on it."""
    board = job.get("board") or {}
    if not board or same_company(job["company"]) in watched():
        return False
    with watchlist_path().open("a", newline="") as f:
        csv.writer(f).writerow([job["company"], board["ats"], board["slug"], "discovered"])
    return True


def main():
    todo = waiting(json.loads(JOBS.read_text()) if JOBS.exists() else {})
    boards, found = {}, {}
    for j in todo:  # network outside the lock
        try:
            found[j["id"]] = find(j["company"], j["title"], boards)
        except Exception as e:  # one bad board shouldn't stop the rest; this role is tried again next time
            print(f"{j['company']}: {type(e).__name__}")
            found[j["id"]] = False
    with locked(JOBS) as store:
        for jid, hit in found.items():
            j = store.get(jid)
            if not j or hit is False:
                continue
            j["link_checked"] = str(date.today())
            if hit:
                j.update(source_url=j["url"], url=hit["url"], link_kind="company", board={"ats": hit["ats"], "slug": hit["slug"]})
    print(f"Checked {len(found)} roles, found {sum(bool(h) for h in found.values())} on company sites")


if __name__ == "__main__":
    main()
