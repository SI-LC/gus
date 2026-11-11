"""Find which public job board a company uses, so anything the discovery agent
turns up can be watched without hand-finding slugs.

Usage:
    python3 -m radar.resolve "Wayve" "PolyAI" "Granola"      # print matches
    python3 -m radar.resolve --add "Wayve" "PolyAI"          # append hits to watchlist.csv
    python3 -m radar.resolve "Acme" --careers https://careers.acme.com     # start from its careers address, the surest way
    python3 -m radar.resolve --missing                       # check every active company with no board; --add appends the hits
    python3 -m radar.resolve --auto                          # the full run's step: add boards found from careers addresses,
                                                             # keep name-guesses for you to look at in the cockpit
"""
from __future__ import annotations

import csv
import http.client
import json
import re
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import urlparse

from radar.boards import board_size, gem_posts, github_count, teamtailor_items, SITE_BOARDS, SOURCES
from radar.paths import watchlist_path, DATA
from radar.web import fetch_bytes, get_json, polite_fetch, site_rules
from radar.store import read

GUESS_DAYS = 14  # how long before a company with no board is guessed at again
# Where a person can look at a board found by name, to see whose it really is
BOARD_URL = {"greenhouse": "https://job-boards.greenhouse.io/{s}", "ashby": "https://jobs.ashbyhq.com/{s}", "lever": "https://jobs.lever.co/{s}",
             "workable": "https://apply.workable.com/{s}", "smartrecruiters": "https://jobs.smartrecruiters.com/{s}", "recruitee": "https://{s}.recruitee.com",
             "githubcareers": "https://www.github.careers/careers-home"}

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
    # the slug is the whole host. These names are Teamtailor's own, and most careers pages mention at least one of them
    "teamtailor": re.compile(r"\b((?!(?:app|www|api|docs|support|career|integrations)\.)[\w-]+(?:\.na)?\.teamtailor\.com)", re.I),
    "gem": re.compile(r"\b(?:jobs\.gem\.com|api\.gem\.com/job_board/v0)/([\w-]+)", re.I),
    # GitHub's own site: one list for the whole company, so the slug is just its name
    "githubcareers": re.compile(r"\b(?:www\.)?(github)\.careers(?![\w.-])", re.I),
}
WORKDAY_LINK = re.compile(r"([\w-]+\.wd\d+)\.myworkdayjobs\.com(?:/[a-z]{2}-[A-Z]{2})?(?:/([\w-]+))?")
CAREERS_HOSTS = re.compile(r"https://((?:careers?|jobs)\.[a-z0-9.-]+)", re.I)
OWN_HOST_BOARDS = ("teamtailor", "successfactors")  # these run on a company's own domain, so the host itself has to be tried


def slugs(name: str):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    words = base.split()
    out = ["".join(words), "-".join(words), words[0] if words else base]
    for suffix in ("hq", "ai", "inc", "labs"):
        out.append("".join(words) + suffix)
    return list(dict.fromkeys(s for s in out if s))


def probe(ats: str, slug: str):
    try:
        if ats == "teamtailor":
            n = len(teamtailor_items(slug))
        elif ats == "gem":
            n = len(gem_posts(slug))
        elif ats == "githubcareers":
            n = github_count()
        elif ats in SITE_BOARDS:
            n = board_size(ats, slug)
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
    """The board behind a careers address: one it names, Teamtailor or SuccessFactors on its own host, or one its page links to."""
    for ats, slug in named_boards(url):
        if hit := try_board(ats, slug):
            return hit
    host = urlparse(url).netloc
    for ats in OWN_HOST_BOARDS:
        if hit := try_board(ats, host):
            return hit
    try:
        page = polite_fetch(url)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    hosts = [h for h in dict.fromkeys(CAREERS_HOSTS.findall(page)) if not h.lower().endswith((".teamtailor.com", "jobs.gem.com"))][:2]
    for ats, slug in named_boards(page)[:5] + [(ats, h) for h in hosts for ats in OWN_HOST_BOARDS]:
        if hit := try_board(ats, slug):
            return hit
    return None


def resolve(name: str, careers: str = None):
    """Return (ats, slug, open_jobs) for the board with the most jobs, or None. SuccessFactors, Workday and Teamtailor boards
    have no address to work out from a name, and a Gem board's reply never says whose it is, so a guess there couldn't be
    checked and might be another company's. All four are only found from a careers address: Gus never guesses at them."""
    if careers and (hit := from_careers(careers)):
        return hit
    tries = [(a, s) for s in slugs(name) for a in PROBES]
    with ThreadPoolExecutor(max_workers=8) as pool:
        hits = list(filter(None, pool.map(lambda t: try_board(*t), tries)))
    return max(hits, key=lambda h: h[2]) if hits else None


def missing() -> dict:
    """Active companies with no board, each with its careers address if it has one."""
    path = DATA / "companies.json"
    companies = read(path)
    return {n: r.get("careers_url") for n, r in companies.items() if r.get("status") in ("watched", "maybe", "candidate") and not r.get("board")}


def address_answers(url: str) -> bool:
    """False only when the address says it's gone (404 or 410). A site that blocks the check still counts as answering."""
    try:
        fetch_bytes(url, timeout=20)
    except urllib.error.HTTPError as e:
        return e.code not in (404, 410)
    except Exception:
        pass
    return True


def add_board(name: str, ats: str, slug: str) -> bool:
    """One more row in watchlist.csv, unless the company is already there."""
    path = watchlist_path()
    if name.lower() in {r["company"].lower() for r in csv.DictReader(path.open())}:
        return False
    with path.open("a", newline="") as f:
        csv.writer(f).writerow([name, ats, slug, "discovered"])
    return True


def chosen(rec: dict) -> bool:
    """An account the candidate picked themselves: a favourite, or one whose tier they set."""
    return bool(rec.get("favourite") or rec.get("tier_locked")) and not rec.get("vetoed")


def restore(companies: dict) -> list:
    """Accounts whose board the scan has stopped reading: watched, or chosen by the candidate, with a board on the record but
    no line in watchlist.csv. A line can go missing when an agent or a person edits the file, and nothing else would notice:
    the account still looks watched. Each board is checked before it goes back. A chosen account parked as maybe or rejected
    is watched again, because the candidate's choice outranks a fit score."""
    listed = {r["company"].lower() for r in csv.DictReader(watchlist_path().open())}
    back = []
    for name, rec in companies.items():
        if rec.get("vetoed") or not (rec.get("status") == "watched" or chosen(rec)):
            continue
        if chosen(rec) and rec.get("status") in ("maybe", "rejected"):
            rec["status"] = "watched"
        ats, _, slug = str(rec.get("board") or "").partition(":")
        if slug and ats in SOURCES and name.lower() not in listed and try_board(ats, slug) and add_board(name, ats, slug):
            listed.add(name.lower())
            back.append(name)
    return back


def _look(name: str, rec: dict):
    """What one company without a board turns up, as (kind, hit, broken): "board" and the board its careers address leads to, "guess"
    and the board its name suggests (or None), or (None, None) when there's nothing new to try. `broken` says its careers address
    no longer answers."""
    careers = rec.get("careers_url")
    if careers:
        broken = not address_answers(careers)
        if not broken and (hit := from_careers(careers)):
            return "board", hit, False
    else:
        broken = False
    if rec.get("board_guess") or (rec.get("guess_checked") or "") > str(date.today() - timedelta(days=GUESS_DAYS)):
        return None, None, broken
    hit = resolve(name)
    if hit and f"{hit[0]}:{hit[1]}" in (rec.get("board_guess_rejected") or []):
        hit = None
    return "guess", hit, broken


def auto() -> dict:
    """The full run's board step. A board named by a company's own careers address is added straight away: Gus
    never guesses there. A board found only from the name is kept on the record as `board_guess` for a person to
    accept or reject in the cockpit, because a name can match another company's board. A careers address that has
    gone is marked `careers_url_broken` so the watchlist agent finds a new one."""
    path = DATA / "companies.json"
    companies = read(path)
    todo = {n: companies[n] for n in missing()}
    out = {"added": [], "guesses": [], "broken": [], "restored": restore(companies)}
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(zip(todo, pool.map(lambda item: _look(*item), todo.items())))
    today = str(date.today())
    for name, (kind, hit, broken) in results:
        rec = companies[name]
        if broken:
            rec["careers_url_broken"] = True
            out["broken"].append(name)
        else:
            rec.pop("careers_url_broken", None)
        if kind == "board":
            ats, slug, _ = hit
            add_board(name, ats, slug)
            rec["board"] = f"{ats}:{slug}"
            out["added"].append(name)
        elif kind == "guess":
            rec["guess_checked"] = today
            if hit:
                ats, slug, n = hit
                rec["board_guess"] = {"board": f"{ats}:{slug}", "url": BOARD_URL.get(ats, "").format(s=slug), "open_roles": n, "found": today}
                out["guesses"].append(name)
    path.write_text(json.dumps(companies, indent=2, ensure_ascii=False))
    if out["added"] or out["restored"]:
        from radar.companies import sync
        sync()
    return out


def main():
    args = sys.argv[1:]
    if "--auto" in args:
        out = auto()
        print(f"{len(out['added'])} boards added from careers addresses: {', '.join(out['added']) or 'none'}")
        if out["restored"]:
            print(f"{len(out['restored'])} boards back on the watchlist after dropping off it: {', '.join(out['restored'])}")
        print(f"{len(out['guesses'])} found by name, waiting for you in the cockpit: {', '.join(out['guesses']) or 'none'}")
        if out["broken"]:
            print(f"careers addresses that have gone: {', '.join(out['broken'])}")
        return
    add = "--add" in args
    careers = args.pop(args.index("--careers") + 1) if "--careers" in args else None
    names = [a for a in args if not a.startswith("--")]
    todo = missing() if "--missing" in args else {n: careers for n in names}
    path = watchlist_path()
    known = {r["company"].lower() for r in csv.DictReader(path.open())}
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(zip(todo, pool.map(lambda t: resolve(*t), todo.items())))
    added = False
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
            added = True
    if added:  # the record gets its `board` now, so the next board step doesn't offer the same board back as a guess
        from radar.companies import sync
        sync()


if __name__ == "__main__":
    main()
