"""Find which public job board a company uses, so anything the discovery agent
turns up can be watched without hand-finding slugs.

Usage:
    python3 -m radar.resolve "Wayve" "PolyAI" "Granola"      # print matches
    python3 -m radar.resolve --add "Wayve" "PolyAI"          # append hits to watchlist.csv
    python3 -m radar.resolve "Acme" --careers https://careers.acme.com     # start from its careers address, the surest way
    python3 -m radar.resolve --missing                       # check every active company with no board; --add appends the hits
"""
from __future__ import annotations

import csv
import http.client
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from radar.scan import DATA, SITE_BOARDS, get_json, listed_jobs, polite_fetch, site_rules, watchlist_path

# ats -> (url for a slug, function that counts jobs in the response)
PROBES = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{s}/jobs", lambda d: len(d.get("jobs", []))),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{s}", lambda d: len(d.get("jobs", []))),
    "lever": ("https://api.lever.co/v0/postings/{s}?mode=json", lambda d: len(d) if isinstance(d, list) else -1),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{s}", lambda d: len(d.get("jobs", []))),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{s}/postings", lambda d: d.get("totalFound", 0)),
    "recruitee": ("https://{s}.recruitee.com/api/offers/", lambda d: len(d.get("offers", []))),
}


# Addresses that name a board outright, on a careers page or as the careers address itself
BOARD_LINKS = {
    "greenhouse": re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I),
    "lever": re.compile(r"jobs(?:\.eu)?\.lever\.co/([\w-]+)", re.I),
    "workable": re.compile(r"apply\.workable\.com/([\w-]+)", re.I),
    "smartrecruiters": re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I),
    "recruitee": re.compile(r"([\w-]+)\.recruitee\.com", re.I),
}
WORKDAY_LINK = re.compile(r"([\w-]+\.wd\d+)\.myworkdayjobs\.com(?:/[a-z]{2}-[A-Z]{2})?(?:/([\w-]+))?")
WORKDAY_SERVERS = ("wd1", "wd3", "wd5", "wd12", "wd103", "wd105", "wd501")
CAREERS_HOSTS = re.compile(r"https://((?:careers|jobs)\.[a-z0-9.-]+)", re.I)


def slugs(name: str):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    words = base.split()
    out = ["".join(words), "-".join(words), words[0] if words else base]
    for suffix in ("hq", "ai", "inc", "labs"):
        out.append("".join(words) + suffix)
    return list(dict.fromkeys(s for s in out if s))


def probe(ats: str, slug: str):
    try:
        if ats in SITE_BOARDS:
            n = len(listed_jobs(ats, slug))
        else:
            url, count = PROBES[ats]
            n = count(get_json(url.format(s=slug)))
    except (OSError, ValueError, http.client.HTTPException):  # Refused is a ValueError: a site that says no has no board for Gus
        return None
    return n if n and n > 0 else None


def try_board(ats: str, slug: str):
    """(ats, slug, open_jobs) if this board answers. A Workday tenant with no site named uses the sites its robots.txt lists."""
    if ats == "workday" and "/" not in slug:
        try:
            sites = [m[1] for m in (re.search(r"/([^/]+)/siteMap\.xml$", u) for u in site_rules(f"{slug}.myworkdayjobs.com")[1]) if m]
        except (OSError, ValueError, http.client.HTTPException):
            return None
        return max(filter(None, (try_board(ats, f"{slug}/{site}") for site in sites[:4])), key=lambda h: h[2], default=None)
    n = probe(ats, slug)
    return (ats, slug, n) if n else None


def named_boards(text: str) -> list:
    """Every board named in an address or a careers page, as (ats, slug), without repeats."""
    found = [("workday", f"{t}/{site}" if site else t) for t, site in WORKDAY_LINK.findall(text)]
    found += [(ats, s) for ats, pattern in BOARD_LINKS.items() for s in pattern.findall(text)]
    return list(dict.fromkeys(found))


def from_careers(url: str):
    """The board behind a careers address: one it names, SuccessFactors on its own host, or one its page links to."""
    for ats, slug in named_boards(url):
        if hit := try_board(ats, slug):
            return hit
    host = urlparse(url).netloc
    if hit := try_board("successfactors", host):
        return hit
    try:
        page = polite_fetch(url)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    for ats, slug in named_boards(page)[:5] + [("successfactors", h) for h in dict.fromkeys(CAREERS_HOSTS.findall(page))][:2]:
        if hit := try_board(ats, slug):
            return hit
    return None


def guesses(name: str) -> list:
    """Enterprise boards to try from a name alone. Their addresses aren't fixed, so these are guesses, kept to the first few spellings."""
    names = slugs(name)[:3]
    return ([("successfactors", f"{sub}.{s}.com") for s in names for sub in ("careers", "jobs")]
            + [("workday", f"{s}.{server}") for s in names for server in WORKDAY_SERVERS])


def resolve(name: str, careers: str = None):
    """Return (ats, slug, open_jobs) for the board with the most jobs, or None. A careers address beats any guess from the name."""
    if careers and (hit := from_careers(careers)):
        return hit
    tries = [(a, s) for s in slugs(name) for a in PROBES] + guesses(name)
    with ThreadPoolExecutor(max_workers=8) as pool:
        hits = list(filter(None, pool.map(lambda t: try_board(*t), tries)))
    return max(hits, key=lambda h: h[2]) if hits else None


def missing() -> dict:
    """Active companies with no board, each with its careers address if it has one."""
    path = DATA / "companies.json"
    companies = json.loads(path.read_text()) if path.exists() else {}
    return {n: r.get("careers_url") for n, r in companies.items() if r.get("status") in ("watched", "maybe", "candidate") and not r.get("board")}


def main():
    args = sys.argv[1:]
    add = "--add" in args
    careers = args.pop(args.index("--careers") + 1) if "--careers" in args else None
    names = [a for a in args if not a.startswith("--")]
    todo = missing() if "--missing" in args else {n: careers for n in names}
    path = watchlist_path()
    known = {r["company"].lower() for r in csv.DictReader(path.open())}
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(zip(todo, pool.map(lambda t: resolve(*t), todo.items())))
    for name, hit in results:
        if not hit:
            print(f"  ✗ {name}: no public board found")
            continue
        ats, slug, n = hit
        status = "already watched" if name.lower() in known else ("added" if add else "found")
        print(f"  ✓ {name}: {ats}:{slug} ({n} open roles) {status}")
        if add and name.lower() not in known:
            with path.open("a", newline="") as f:
                csv.writer(f).writerow([name, ats, slug, "discovered"])
            known.add(name.lower())


if __name__ == "__main__":
    main()
