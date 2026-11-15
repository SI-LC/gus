"""Each company's own website, so its name in the cockpit can open it.

A website is only kept once its own homepage shows it belongs to the company: it links to the company's job board or
careers address, or, for a company with neither, its page title carries the company's name. Candidates come from what Gus
already holds (the careers address, the research links), then the obvious guesses. Every page is read with `polite_fetch`,
so a site's robots.txt is followed. A website you set yourself (`website_locked`) is never replaced.

Usage:
    python3 -m radar.websites            # the scan's step: up to BATCH companies due a look
    python3 -m radar.websites "Acme"     # one company, now, whatever its last check
"""
from __future__ import annotations

import http.client
import re
import socket
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import urlparse

from radar import web
from radar.paths import DATA
from radar.store import locked, read

COMPANIES = DATA / "companies.json"
BATCH = 50  # companies looked at per scan, so the first fill of a long list spreads over a few days
RECHECK_DAYS = 30  # how long before a company is looked at again: a website is re-checked, a miss is tried again
TIME_PER_COMPANY = 20  # seconds, all its fetches together
ENDINGS = (".com", ".ai", ".io")
MAX_CANDIDATES = 5
ACTIVE = ("watched", "maybe", "candidate")
SUBDOMAIN = re.compile(r"^(?:www|careers?|jobs|about|go|app)\.", re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# Hosts that are never a company's own site, however a research link or careers address points there
NOT_OWN = re.compile(r"(?:^|\.)(?:greenhouse\.io|ashbyhq\.com|lever\.co|workable\.com|smartrecruiters\.com|recruitee\.com|teamtailor\.com|"
                     r"myworkdayjobs\.com|gem\.com|linkedin\.com|github\.com|medium\.com|substack\.com|crunchbase\.com|wikipedia\.org|"
                     r"x\.com|twitter\.com|youtube\.com|google\.com|yahoo\.com|bloomberg\.com|techcrunch\.com|businesswire\.com)$", re.I)


def words(name: str) -> list:
    return re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()


def own_host(url: str) -> str:
    """The company's main host behind an address: careers.acme.com and www.acme.com are both acme.com."""
    host = urlparse(url or "").netloc.lower().split(":")[0]
    while SUBDOMAIN.match(host) and host.count(".") > 1:
        host = SUBDOMAIN.sub("", host, 1)
    return "" if not host or NOT_OWN.search(host) else host


def candidates(name: str, rec: dict) -> list:
    """Homepages to try, surest first: the careers address's host, research links whose host carries the name, then guesses."""
    joined = "".join(words(name))
    research = [p.get("url") for p in rec.get("product_leaders") or [] if isinstance(p, dict)]
    hosts = [own_host(rec.get("careers_url"))]
    hosts += [h for h in map(own_host, research) if joined and joined in h.replace("-", "")]
    hosts += [joined + e for e in ENDINGS] if joined else []
    return [f"https://{h}/" for h in dict.fromkeys(filter(None, hosts))][:MAX_CANDIDATES]


def belongs(page: str, name: str, rec: dict, sitemap: list = ()) -> bool:
    """Whether a homepage is this company's: it links to the company's board or careers address. A company with neither is
    matched by its name in the page title, word for word."""
    from radar.resolve import named_boards
    text = page + " " + " ".join(sitemap)
    ats, _, slug = str(rec.get("board") or "").partition(":")
    careers = (rec.get("careers_url") or "").split("://")[-1].rstrip("/").lower()
    if slug and (ats, slug.lower()) in {(a, s.lower()) for a, s in named_boards(text)}:
        return True
    if careers and careers in text.lower():
        return True
    if slug or careers:
        return False
    title = TITLE.search(page)
    return bool(title) and re.search(r"(?<![\w-])" + r"\W+".join(map(re.escape, words(name))) + r"(?![\w-])",
                                     web.strip_html(title[1]), re.I) is not None


def find(name: str, rec: dict):
    """The company's website, as https://host/, or None when no candidate shows it belongs to the company."""
    web.time_limit(time.monotonic() + TIME_PER_COMPANY)
    try:
        for url in candidates(name, rec):
            try:
                page = web.polite_fetch(url)
            except (OSError, ValueError, http.client.HTTPException):  # gone, refused by robots.txt, or out of time
                continue
            if belongs(page, name, rec):
                return url
            if (rec.get("board") or rec.get("careers_url")) and belongs(page, name, rec, sitemap_of(url)):
                return url
        return None
    finally:
        web.time_limit(None)


def sitemap_of(url: str) -> list:
    """The first sitemap a site's robots.txt lists, for a homepage that doesn't link to the board itself."""
    try:
        listed = web.site_rules(urlparse(url).netloc)[1][:1]
        return web.sitemap_urls(listed[0], follow=False) if listed else []
    except (OSError, ValueError, http.client.HTTPException):
        return []


def gone(url: str) -> bool:
    """True only when the site says it's gone (404 or 410) or its name no longer exists. A site that blocks the check is still there."""
    web.time_limit(time.monotonic() + TIME_PER_COMPANY)
    try:
        web.fetch_bytes(url)
    except urllib.error.HTTPError as e:
        return e.code in (404, 410)
    except urllib.error.URLError as e:
        return isinstance(e.reason, socket.gaierror)
    except Exception:
        pass
    finally:
        web.time_limit(None)
    return False


def due(rec: dict, today: date) -> bool:
    return rec.get("status") in ACTIVE and not rec.get("vetoed") and (rec.get("website_checked") or "") <= str(today - timedelta(days=RECHECK_DAYS))


def look(name: str, rec: dict) -> dict:
    """What one company's check found: {"website": url or None, "broken": bool}."""
    site = rec.get("website")
    if not site and rec.get("website_locked"):  # you cleared it: no website is your answer
        return {"website": None, "broken": False}
    if site and not gone(site):
        return {"website": site, "broken": False}
    if site and rec.get("website_locked"):
        return {"website": site, "broken": True}
    found = find(name, rec)
    return {"website": found or site, "broken": bool(site and not found)}


def save(results: dict, today: date) -> None:
    """Write what the checks found. A website you set while they ran is yours, and stays."""
    with locked(COMPANIES) as companies:
        for name, got in results.items():
            rec = companies.get(name)
            if rec is None:
                continue
            rec["website_checked"] = str(today)
            if rec.get("website_locked") and rec.get("website") != got["website"]:
                continue
            if got["website"]:
                rec["website"] = got["website"]
            if got["broken"]:
                rec["website_broken"] = True
            else:
                rec.pop("website_broken", None)


def run(names: list = None, limit: int = BATCH) -> dict:
    from radar.accounts import tier
    today, companies = date.today(), read(COMPANIES)
    if names:
        todo = [n for n in names if n in companies]
    else:  # tier A first, and companies never looked at before ones being re-checked
        todo = sorted((n for n, r in companies.items() if due(r, today)),
                      key=lambda n: ("ABC-".find(tier(companies[n])), bool(companies[n].get("website_checked"))))[:limit]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(zip(todo, pool.map(lambda n: look(n, companies[n]), todo)))
    save(results, today)
    found = [n for n in todo if results[n]["website"] and not companies[n].get("website")]
    broken = [n for n in todo if results[n]["broken"]]
    return {"looked": len(todo), "found": found, "broken": broken}


def main():
    out = run(sys.argv[1:] or None)
    print(f"{out['looked']} companies looked at, {len(out['found'])} websites found: {', '.join(out['found']) or 'none'}")
    if out["broken"]:
        print(f"websites that have gone: {', '.join(out['broken'])}")


if __name__ == "__main__":
    main()
