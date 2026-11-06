"""Poll public job-board APIs for the watchlist, keep target roles in the target
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
import threading
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from radar.settings import group_of, groups, load
from radar.store import locked

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS = DATA / "jobs.json"
WATCHLIST = ROOT / "watchlist.csv"  # yours, git-ignored; starts as a copy of watchlist.example.csv
UA = {"User-Agent": "gus/0.1 (+https://github.com/)", "Accept-Encoding": "gzip"}


MAX_RESPONSE = 50_000_000


_limit = threading.local()


def time_limit(deadline) -> None:
    """Fetches on this thread give up at this moment (time.monotonic), with no retries. None goes back to normal."""
    _limit.deadline = deadline


def _fetch_by(req, deadline) -> bytes:
    """One try that stops at the deadline even if a server keeps trickling data, which a socket timeout alone wouldn't."""
    with urllib.request.urlopen(req, timeout=max(0.5, deadline - time.monotonic())) as r:
        parts, size = [], 0
        while chunk := r.read(65536):
            size += len(chunk)
            if time.monotonic() > deadline or size > MAX_RESPONSE:
                raise TimeoutError("Ran out of time")
            parts.append(chunk)
        data = b"".join(parts)
        if r.headers.get("Content-Encoding") == "gzip":
            data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, MAX_RESPONSE + 1)
    return data


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    """Web sources only, with a size limit, so a bad feed or a redirect can't reach local files or fill memory."""
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError(f"Not a web address: {url[:60]}")
    req = urllib.request.Request(url, headers=UA)
    if getattr(_limit, "deadline", None) is not None:
        return _fetch_by(req, _limit.deadline)
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


# --- labels: how Role Inbox groups roles, from Settings -----------------------------------------------

def where(location: str, rows=None) -> str:
    return group_of(location, groups(load()["where_groups"]) if rows is None else rows)


def role_type(title: str, rows=None) -> str:
    return group_of(title, groups(load()["role_kinds"]) if rows is None else rows)


# --- sources: each returns a list of normalised job dicts --------------------

def greenhouse(company: str, slug: str):
    """Big boards list hundreds of jobs. Get titles first, then full details only for new target roles."""
    cfg = load_config()
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    known = known_jobs()
    out = []
    for j in get_json(base).get("jobs", []):
        title = j["title"]
        _listed.add(f"gh:{slug}:{j['id']}")  # every live job, even titles the filter drops, so nothing looks closed by mistake
        if not wanted_title(title, cfg):
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
    """Latest 'Ask HN: Who is hiring?' thread: top-level comments, each filtered by matches()."""
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
    from radar.settings import PATTERN_ENDINGS, to_pattern
    return {k: re.compile(to_pattern(v), re.I) if k.endswith(PATTERN_ENDINGS) else v for k, v in load().items()}


def left_out(text, cfg, key) -> bool:
    """On the leave-out list, once its exceptions are ignored: "engineer" mustn't catch "product engineer"."""
    keep = cfg.get("title_exclude_except" if key == "title_exclude" else "source_company_except")
    return bool(cfg[key].search(keep.sub(" ", text) if keep else text))


def wanted_title(title, cfg) -> bool:
    return bool(cfg["title_include"].search(title)) and not left_out(title, cfg, "title_exclude")


def matches(job, cfg, hn=False) -> bool:
    title = job["title"]
    if hn:  # HN posts are free text, so test the whole comment
        words = cfg["hn_title_include"] if cfg["hn_title_include"].pattern != "(?!)" else cfg["title_include"]
        return bool(words.search(title + job["description"])) and bool(cfg["location_include"].search(job["description"]))
    place = " / ".join(dict.fromkeys(p.strip() for p in job["location"].split("/")))  # "Remote / Remote" is just remote
    return wanted_title(title, cfg) and bool(cfg["location_include"].search(place))


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
