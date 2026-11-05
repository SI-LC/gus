"""Poll public job-board APIs for the watchlist, keep PM roles in the target
locations, and record anything not seen before.

Usage:
    python3 -m radar.scan            # scan everything, print new roles
    python3 -m radar.scan --json     # same, as JSON (for the Claude triage step)
"""
from __future__ import annotations

import csv
import html
import http.client
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from radar.store import locked

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS = DATA / "jobs.json"
WATCHLIST = ROOT / "watchlist.csv"  # yours, git-ignored; starts as a copy of watchlist.example.csv
UA = {"User-Agent": "gus/0.1 (+https://github.com/)", "Accept-Encoding": "gzip"}


MAX_RESPONSE = 50_000_000


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    """Web sources only, with a size limit, so a bad feed or a redirect can't reach local files or fill memory."""
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError(f"Not a web address: {url[:60]}")
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(3):  # wifi connections drop now and then; a 4xx won't change, so don't retry those
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(MAX_RESPONSE + 1)
                if r.headers.get("Content-Encoding") == "gzip":
                    d = zlib.decompressobj(16 + zlib.MAX_WBITS)  # bounded, so a tiny download can't expand without limit
                    data = d.decompress(data, MAX_RESPONSE + 1)
            break
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    if len(data) > MAX_RESPONSE:
        raise ValueError(f"Response too large: {url[:60]}")
    return data


_known = None
_listed: set = set()
# Boards that list every job, so a missing role really has gone. SmartRecruiters is left out: only its first 100 come back.
PREFIX = {"greenhouse": "gh", "ashby": "ashby", "lever": "lever", "workable": "workable", "recruitee": "recruitee"}
MISSES_TO_CLOSE = 2


def known_jobs() -> dict:
    """Roles already saved. Read once per scan."""
    global _known
    if _known is None:
        _known = json.loads(JOBS.read_text()) if JOBS.exists() else {}
    return _known


def get_json(url: str):
    return json.loads(fetch_bytes(url))


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


# --- labels: London stays the focus; other places and role types stay visible, not mixed in ---------

def where(location: str) -> str:
    loc = (location or "").lower()
    if "london" in loc:
        return "London"
    if re.search(r"remote|anywhere", loc):
        return "Remote"
    if re.search(r"united kingdom|\buk\b|england|scotland|wales", loc):
        return "UK"
    return "Europe"


def role_type(title: str) -> str:
    t = (title or "").lower()
    if "product manager" in t and "forward" not in t:
        return "PM"
    if re.search(r"forward.deployed|deployed (staff|engineer|pm)", t):
        return "Forward deployed"
    if re.search(r"product (engineer|builder)|founding product|applied ai", t):
        return "Product engineer"
    return "PM"


# --- sources: each returns a list of normalised job dicts --------------------

def greenhouse(company: str, slug: str):
    """Big boards list hundreds of jobs. Get titles first, then full details only for new PM roles."""
    cfg = load_config()
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    known = known_jobs()
    out = []
    for j in get_json(base).get("jobs", []):
        title = j["title"]
        _listed.add(f"gh:{slug}:{j['id']}")  # every live job, even titles the filter drops, so nothing looks closed by mistake
        if not cfg["title_include"].search(title) or cfg["title_exclude"].search(title):
            continue
        jid = f"gh:{slug}:{j['id']}"
        row = {"id": jid, "company": company, "title": title, "url": j["absolute_url"],
               "posted": j.get("first_published") or j.get("updated_at"), "description": "",
               "location": j.get("location", {}).get("name", "")}
        if jid in known:  # already saved: only needs to count as still live
            row["location"] = known[jid].get("location") or row["location"]
        else:
            d = get_json(f"{base}/{j['id']}")
            # some boards (e.g. Cloudflare) put "Hybrid" in location and the cities in offices
            row["location"] = " / ".join(dict.fromkeys(filter(None, [row["location"]] + [o.get("name", "") for o in d.get("offices") or []])))
            row["description"] = strip_html(d.get("content", ""))
        out.append(row)
    return out


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


_boards_answered: set = set()


def mark_closed(store: dict, answered: set, live: set, now: str) -> list:
    """Roles gone from a board that answered, two scans running, are marked closed; if one returns, it's reopened."""
    closed = []
    for jid, j in store.items():
        board = ":".join(jid.split(":")[:2])
        if board not in answered:
            continue
        if jid in live:
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
    tasks = [(r["company"], r["ats"], r["slug"]) for r in rows if r["ats"] in SOURCES]
    errors, jobs = [], []

    def run(t):
        company, ats, slug = t
        try:
            return t, SOURCES[ats](company, slug), None
        except Exception as e:  # one dead board shouldn't stop the scan
            return t, [], (company, f"{ats}:{slug}: {e}")

    answered = set()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for (_, ats, slug), found, err in pool.map(run, tasks):
            jobs += found
            if err:
                errors.append(err)
            elif ats in PREFIX:
                answered.add(f"{PREFIX[ats]}:{slug}")
    _boards_answered.clear()
    _boards_answered.update(answered)
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
    live = {j["id"] for j in jobs} | _listed
    with locked(JOBS) as store:
      mark_closed(store, _boards_answered, live, now)
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
