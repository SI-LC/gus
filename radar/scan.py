"""Poll public job-board APIs for the watchlist, keep PM roles in the target
locations, and record anything not seen before.

Usage:
    python3 -m radar.scan            # scan everything, print new roles
    python3 -m radar.scan --json     # same, as JSON (for the Claude triage step)
"""
from __future__ import annotations

import csv
import html
import json
import re
import shutil
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from radar.store import locked

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS = DATA / "jobs.json"
WATCHLIST = ROOT / "watchlist.csv"  # yours, git-ignored; starts as a copy of watchlist.example.csv
UA = {"User-Agent": "pm-radar/0.1 (+https://github.com/)"}


MAX_RESPONSE = 50_000_000


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    """Web sources only, with a size limit, so a bad feed or a redirect can't reach local files or fill memory."""
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError(f"Not a web address: {url[:60]}")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError(f"Response too large: {url[:60]}")
    return data


def get_json(url: str):
    return json.loads(fetch_bytes(url))


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


# --- sources: each returns a list of normalised job dicts --------------------

def greenhouse(company: str, slug: str):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [
        {
            "id": f"gh:{slug}:{j['id']}",
            "company": company,
            "title": j["title"],
            # some boards (e.g. Cloudflare) put "Hybrid" in location and the cities in offices
            "location": " / ".join(dict.fromkeys(filter(None, [j.get("location", {}).get("name", "")]
                                                         + [o.get("name", "") for o in j.get("offices") or []]))),
            "url": j["absolute_url"],
            "posted": j.get("first_published") or j.get("updated_at"),
            "description": strip_html(j.get("content", "")),
        }
        for j in d.get("jobs", [])
    ]


def ashby(company: str, slug: str):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in d.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location", "")] + [l.get("location", "") for l in j.get("secondaryLocations") or []]
        out.append({
            "id": f"ashby:{slug}:{j['id']}",
            "company": company,
            "title": j["title"],
            "location": " / ".join(filter(None, locs)),
            "url": j["jobUrl"],
            "posted": j.get("publishedAt"),
            "description": j.get("descriptionPlain", ""),
        })
    return out


def lever(company: str, slug: str):
    d = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [
        {
            "id": f"lever:{slug}:{j['id']}",
            "company": company,
            "title": j["text"],
            "location": " / ".join(j.get("categories", {}).get("allLocations") or [j.get("categories", {}).get("location", "")]),
            "url": j["hostedUrl"],
            "posted": datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat(),
            "description": j.get("descriptionPlain", ""),
        }
        for j in d
    ]


def workable(company: str, slug: str):
    d = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    return [
        {
            "id": f"workable:{slug}:{j['shortcode']}",
            "company": company,
            "title": j["title"],
            "location": ", ".join(filter(None, [j.get("city"), j.get("country")])) + (" / Remote" if j.get("telecommuting") else ""),
            "url": j["url"],
            "posted": j.get("published_on"),
            "description": "",
        }
        for j in d.get("jobs", [])
    ]


def smartrecruiters(company: str, slug: str):
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    out = []
    for j in d.get("content", []):
        loc = j.get("location", {})
        out.append({
            "id": f"sr:{slug}:{j['id']}",
            "company": company,
            "title": j["name"],
            "location": ", ".join(filter(None, [loc.get("city"), loc.get("country")])) + (" / Remote" if loc.get("remote") else ""),
            "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
            "posted": j.get("releasedDate"),
            "description": "",
        })
    return out


def recruitee(company: str, slug: str):
    d = get_json(f"https://{slug}.recruitee.com/api/offers/")
    return [
        {
            "id": f"recruitee:{slug}:{j['id']}",
            "company": company,
            "title": j["title"],
            "location": j.get("location", ""),
            "url": j["careers_url"],
            "posted": j.get("published_at"),
            "description": strip_html(j.get("description", "")),
        }
        for j in d.get("offers", [])
    ]


def hn_who_is_hiring():
    """Latest 'Ask HN: Who is hiring?' thread: top-level comments mentioning PM + UK."""
    s = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring")
    story = next(h for h in s["hits"] if h["title"].lower().startswith("ask hn: who is hiring"))
    thread = get_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    out = []
    for c in thread.get("children", []):
        text = strip_html(c.get("text") or "")
        if not text:
            continue
        first = text.split("|")[0].strip()[:60]
        out.append({
            "id": f"hn:{c['id']}",
            "company": first,
            "title": text[:160],
            "location": text[:300],
            "url": f"https://news.ycombinator.com/item?id={c['id']}",
            "posted": c.get("created_at"),
            "description": text,
        })
    return out


SOURCES = {"greenhouse": greenhouse, "ashby": ashby, "lever": lever, "workable": workable,
           "smartrecruiters": smartrecruiters, "recruitee": recruitee}


# --- pipeline ----------------------------------------------------------------

def watchlist_path() -> Path:
    if not WATCHLIST.exists():
        shutil.copy(ROOT / "watchlist.example.csv", WATCHLIST)
    return WATCHLIST


def load_config():
    from radar.settings import load
    return {k: re.compile(v, re.I) if k.endswith(("include", "exclude")) else v for k, v in load().items()}


def matches(job, cfg, hn=False) -> bool:
    title = job["title"]
    if hn:  # HN posts are free text, so test the whole comment
        return bool(re.search(r"product manager", title + job["description"], re.I)) and bool(
            cfg["location_include"].search(job["description"]))
    return (bool(cfg["title_include"].search(title))
            and not cfg["title_exclude"].search(title)
            and bool(cfg["location_include"].search(job["location"])))


def fetch_all():
    rows = list(csv.DictReader(watchlist_path().open()))
    tasks = [(r["company"], r["ats"], r["slug"]) for r in rows if r["ats"] in SOURCES]
    errors, jobs = [], []

    def run(t):
        company, ats, slug = t
        try:
            return SOURCES[ats](company, slug), None
        except Exception as e:  # one dead board shouldn't stop the scan
            return [], (company, f"{ats}:{slug}: {e}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        for found, err in pool.map(run, tasks):
            jobs += found
            if err:
                errors.append(err)
    (DATA / "board_errors.json").write_text(json.dumps(dict(e for e in errors if isinstance(e, tuple))))
    errors = [e if isinstance(e, str) else f"{e[0]} ({e[1]})" for e in errors]
    if load_config().get("source_hn", True):
        try:
            jobs += [dict(j, _hn=True) for j in hn_who_is_hiring()]
        except Exception as e:
            errors.append(f"HN: {e}")
    return jobs, errors


def main():
    cfg = load_config()
    DATA.mkdir(exist_ok=True)
    jobs, errors = fetch_all()  # slow network part, outside the lock
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new = []
    with locked(JOBS) as store:
      for j in jobs:
        hn = j.pop("_hn", False)
        if not matches(j, cfg, hn):
            continue
        if j["id"] in store:
            store[j["id"]]["last_seen"] = now
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
