"""Find which public job board a company uses, so anything the discovery agent
turns up can be watched without hand-finding slugs.

Usage:
    python3 -m radar.resolve "Wayve" "PolyAI" "Granola"      # print matches
    python3 -m radar.resolve --add "Wayve" "PolyAI"          # append hits to watchlist.csv
"""
from __future__ import annotations

import csv
import re
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from radar.scan import ROOT, get_json, watchlist_path

# ats -> (url for a slug, function that counts jobs in the response)
PROBES = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{s}/jobs", lambda d: len(d.get("jobs", []))),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{s}", lambda d: len(d.get("jobs", []))),
    "lever": ("https://api.lever.co/v0/postings/{s}?mode=json", lambda d: len(d) if isinstance(d, list) else -1),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{s}", lambda d: len(d.get("jobs", []))),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{s}/postings", lambda d: d.get("totalFound", 0)),
    "recruitee": ("https://{s}.recruitee.com/api/offers/", lambda d: len(d.get("offers", []))),
}


def slugs(name: str):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    words = base.split()
    out = ["".join(words), "-".join(words), words[0] if words else base]
    for suffix in ("hq", "ai", "inc", "labs"):
        out.append("".join(words) + suffix)
    return list(dict.fromkeys(s for s in out if s))


def probe(ats: str, slug: str):
    url, count = PROBES[ats]
    try:
        n = count(get_json(url.format(s=slug)))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError):
        return None
    return n if n and n > 0 else None


def resolve(name: str):
    """Return (ats, slug, open_jobs) for the board with the most jobs, or None."""
    tries = [(a, s) for s in slugs(name) for a in PROBES]
    with ThreadPoolExecutor(max_workers=8) as pool:
        hits = [(a, s, n) for (a, s), n in zip(tries, pool.map(lambda t: probe(*t), tries)) if n]
    return max(hits, key=lambda h: h[2]) if hits else None


def main():
    args = [a for a in sys.argv[1:] if a != "--add"]
    add = "--add" in sys.argv
    path = watchlist_path()
    known = {r["company"].lower() for r in csv.DictReader(path.open())}
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(zip(args, pool.map(resolve, args)))
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
