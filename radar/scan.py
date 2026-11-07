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
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from radar.settings import group_of, groups, load
from radar.store import locked

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS = DATA / "jobs.json"
LOOKED_UP = DATA / "board_locations.json"
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


def fetch_bytes(url: str, timeout: int = 30, body: bytes = None, headers: dict = None) -> bytes:
    """Web sources only, with a size limit, so a bad feed or a redirect can't reach local files or fill memory."""
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError(f"Not a web address: {url[:60]}")
    req = urllib.request.Request(url, data=body, headers={**UA, **(headers or {})})
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
_looked_up = None
_listed: set = set()
# Boards that list every job, so a missing role really has gone. SmartRecruiters is left out: only its first 100 come back.
PREFIX = {"greenhouse": "gh", "ashby": "ashby", "lever": "lever", "workable": "workable", "recruitee": "recruitee",
          "successfactors": "sf", "workday": "wd", "teamtailor": "tt"}
MISSES_TO_CLOSE = 2


def known_jobs() -> dict:
    """Roles already saved. Read once per scan."""
    global _known
    if _known is None:
        _known = json.loads(JOBS.read_text()) if JOBS.exists() else {}
    return _known


def looked_up() -> dict:
    """Where Greenhouse roles we've already fetched are based. Read once per scan. A role edited since is fetched again."""
    global _looked_up
    if _looked_up is None:
        _looked_up = json.loads(LOOKED_UP.read_text()) if LOOKED_UP.exists() else {}
    return _looked_up


def save_looked_up(answered: set) -> None:
    """Forget roles that have gone from a board. Boards that didn't answer this time keep theirs."""
    keep = {k: v for k, v in looked_up().items() if ":".join(k.split(":")[:2]) not in answered or k in _listed}
    LOOKED_UP.write_text(json.dumps(keep))


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
        looked = looked_up().get(jid)
        if jid in known:  # already saved: only needs to count as still live
            row["location"] = known[jid].get("location") or row["location"]
        elif looked and looked["updated"] == j.get("updated_at") and not matches({**row, "location": looked["location"]}, cfg):
            continue  # a role elsewhere is never saved, so without this its details would be fetched again every scan
        else:
            d = get_json(f"{base}/{j['id']}")
            # some boards (e.g. Cloudflare) put "Hybrid" in location and the cities in offices
            row["location"] = " / ".join(dict.fromkeys(filter(None, [row["location"]] + [o.get("name", "") for o in d.get("offices") or []])))
            row["description"] = strip_html(d.get("content", ""))
            looked_up()[jid] = {"updated": j.get("updated_at"), "location": row["location"]}
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


# --- company careers sites: what each site publishes, within its robots.txt ------------------------------

class Refused(ValueError):
    """A site said no: a robots.txt rule, a refused request or a bot check. Reported for the board, never worked around."""


PAGE_PAUSE = 1.5  # seconds between job pages on one board
PAGES_PER_SCAN = 25  # job pages read per board; the rest wait for the next scan
RETRY_UNREAD_DAYS = 7  # a job page that made no sense is tried again after this long
UNREAD_TO_FLAG = 3  # this many unreadable pages, and none readable, means the board itself is the problem
GONE = "gone"  # the site said the ad no longer exists
WORKDAY_LIST_PAGES = 50  # of 20 jobs each, so 1,000 jobs in your locations; a longer list is cut short
SAVED_CHECKS = 10  # saved roles missing from a Workday list whose own page is checked each scan
WORKDAY_JSON = {"Accept": "application/json", "Content-Type": "application/json"}
ROBOTS_KEEP = 3600  # seconds to trust a robots.txt
BOT_CHECK = re.compile(r"captcha|just a moment|cf-chl|attention required|access denied", re.I)
PUBLIC_HOST = re.compile(r"(?:[a-z0-9-]+\.)+(?!local$|internal$|lan$|home$)[a-z]{2,}", re.I)  # no IP addresses, ports or home network names
_robots: dict = {}
_cut_short: set = set()  # boards whose list didn't finish, so a missing role proves nothing
COUNTRIES = {"GB": "United Kingdom", "IE": "Ireland", "US": "United States", "FR": "France", "DE": "Germany", "NL": "Netherlands",
             "ES": "Spain", "SE": "Sweden", "DK": "Denmark", "NO": "Norway", "FI": "Finland"}  # Teamtailor gives codes; others stay as codes


def parse_robots(text: str) -> tuple:
    """The rules that apply to Gus and the sitemaps listed: the group naming "gus", otherwise the "*" group (RFC 9309)."""
    found, sitemaps, agents, in_rules = {}, [], [], False
    for line in text.splitlines():
        field, _, value = line.split("#")[0].partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "sitemap":
            sitemaps.append(value)
        elif field == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
        elif field in ("allow", "disallow"):
            in_rules = True
            for agent in agents if value else ():
                found.setdefault(agent, []).append((field == "allow", value))
    return found.get("gus", found.get("*", [])), sitemaps


def robots_allow(rules: list, path: str) -> bool:
    """The longest matching rule decides and Allow wins a tie, which is why this isn't urllib.robotparser (first match wins)."""
    def hit(rule):
        ends = rule.endswith("$")
        pattern = re.escape(rule[:-1] if ends else rule).replace(r"\*", ".*")
        return re.match(pattern + ("$" if ends else ""), path)
    return max(((len(rule), allow) for allow, rule in rules if hit(rule)), default=(0, True))[1]


def site_rules(host: str) -> tuple:
    """A site's robots.txt, as (rules, sitemaps). A missing file means no rules. If it can't be read, nothing is fetched."""
    kept = _robots.get(host)
    if kept and kept[0] > time.monotonic():
        return kept[1]
    try:
        rules = parse_robots(fetch_bytes(f"https://{host}/robots.txt").decode("utf-8-sig", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429) or e.code >= 500:
            raise Refused(f"robots.txt answered {e.code}") from e
        rules = ([], [])
    _robots[host] = (time.monotonic() + ROBOTS_KEEP, rules)
    return rules


def polite_fetch(url: str, body: bytes = None) -> str:
    """A page from a public site whose robots.txt doesn't rule it out. Workday's job data is the one thing that needs its own headers."""
    parts = urlparse(url)
    if parts.scheme != "https" or not PUBLIC_HOST.fullmatch(parts.netloc):
        raise Refused(f"Not a public https address: {url[:60]}")
    if not robots_allow(site_rules(parts.netloc)[0], parts.path + (f"?{parts.query}" if parts.query else "")):
        raise Refused(f"robots.txt disallows {parts.path[:60]}")
    return fetch_bytes(url, body=body, headers=WORKDAY_JSON if "/wday/cxs/" in parts.path else None).decode("utf-8", "ignore")


def sitemap_urls(url: str, follow: bool = True) -> list:
    """Every address in a sitemap. Parsed by pattern, not as XML, so a hostile file can't expand itself."""
    text = polite_fetch(url)
    locs = [html.unescape(u) for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)]
    if "<sitemapindex" in text and follow:
        host = urlparse(url).netloc
        return [u for child in locs if urlparse(child).netloc == host for u in sitemap_urls(child, False)]
    if "<urlset" not in text:
        raise Refused("No sitemap came back: it may be blocked or behind a bot check")
    return locs


def successfactors_job(url: str):
    """(job id, words to test) from an address like /job/London-Product-Manager-WC2N-4JS/1368804655/. The words include the place."""
    m = re.search(r"/job/([^/]+)/(\d+)/?$", urlparse(url).path)
    return (m[2], re.sub(r"[-_]+", " ", unquote(m[1]))) if m else None


def workday_job(url: str):
    """(job id, words to test) from an address like /Site/job/United-Kingdom---London/Product-Manager_JR1234."""
    m = re.search(r"/job/(?:[^/]+/)?([^/]+)_([^/_]+)/?$", urlparse(url).path)
    return (m[2], re.sub(r"[-_]+", " ", unquote(m[1]))) if m else None


class _Tagged(HTMLParser):
    """The text inside the element carrying itemprop="description". Only that tag is counted, as job ads often leave <p> open."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tag, self.depth, self.text = None, 0, []

    def handle_starttag(self, tag, attrs):
        if self.depth and tag == self.tag:
            self.depth += 1
        elif not self.tag and dict(attrs).get("itemprop") == "description":
            self.tag, self.depth = tag, 1

    def handle_endtag(self, tag):
        if self.depth and tag == self.tag:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.text.append(data)


def successfactors_page(text: str):
    """Title, place, date and description from a job page, or None if it holds no job (an expired one shows an error page)."""
    title = re.search(r'itemprop="title"[^>]*>([^<]+)<', text)
    place = re.search(r'itemprop="streetAddress"\s+content="([^"]*)"', text)
    if not title or not place:
        return None
    posted = re.search(r'itemprop="datePosted"\s+content="([^"]*)"', text)
    try:
        posted = datetime.strptime(posted[1], "%a %b %d %H:%M:%S %Z %Y").date().isoformat() if posted else None
    except ValueError:
        posted = None
    body = _Tagged()
    body.feed(text)
    return {"title": html.unescape(title[1]).strip(), "posted": posted, "description": re.sub(r"\s+", " ", " ".join(body.text)).strip(),
            "location": re.sub(r",\s*GB\b", ", United Kingdom", html.unescape(place[1]))}


def workday_page(text: str):
    """Title, every place, date and description from a job's data. The public job page names only the first place."""
    d = json.loads(text).get("jobPostingInfo")
    if not d or d.get("posted") is False:
        return None
    return {"title": d.get("title", ""), "posted": d.get("startDate"), "description": strip_html(d.get("jobDescription", "")),
            "location": " / ".join(filter(None, [d.get("location"), *(d.get("additionalLocations") or [])]))}


def successfactors_list(slug: str) -> dict:
    """{job id: (address, words to test)} from the sitemap. The slug is the careers host."""
    found = {}
    for url in sitemap_urls(f"https://{slug}/sitemap.xml"):
        if hit := successfactors_job(url):
            found[hit[0]] = (url, hit[1])
    if not found:  # an empty list is an error, or every saved role would look closed
        raise Refused("Its sitemap lists no jobs")
    return found


def workday_data(slug: str) -> str:
    """Where a board's job data lives. The slug is tenant.wdN/site."""
    host, site = slug.split("/", 1)
    return f"https://{host}.myworkdayjobs.com/wday/cxs/{host.split('.')[0]}/{site}"


def workday_jobs(slug: str, filters: dict = None, offset: int = 0) -> dict:
    """One page of 20 jobs. Only the first page gives the total. With no filters it also describes the filters the board offers."""
    ask = {"appliedFacets": filters or {}, "limit": 20, "offset": offset, "searchText": ""}
    return json.loads(polite_fetch(f"{workday_data(slug)}/jobs", json.dumps(ask).encode()))


def location_filters(facets: list, cfg) -> tuple:
    """({filter: [ids of the places your location rules want]}, whether the board has a location filter at all).
    Each company names its filters its own way, so they're found by name: anything about locations or countries."""
    wanted, any_found = {}, False
    for f in facets or []:
        values = f.get("values") or []
        if f.get("facetParameter") and re.search(r"location|country", f"{f['facetParameter']} {f.get('descriptor') or ''}", re.I):
            any_found = any_found or any(v.get("id") for v in values)
            if ids := [v["id"] for v in values if v.get("id") and cfg["location_include"].search(v.get("descriptor") or "")]:
                wanted[f["facetParameter"]] = ids
        inner, inner_found = location_filters([v for v in values if "values" in v], cfg)  # a group of filters
        wanted.update(inner)
        any_found = any_found or inner_found
    return wanted, any_found


def workday_list(slug: str) -> dict:
    """{job id: (address, title)} for jobs in your locations only. The board says which places it has, your location rules
    pick from them, and only those jobs are asked for: a handful of requests where the whole list could be a hundred."""
    host, site = slug.split("/", 1)
    filters, any_found = location_filters(workday_jobs(slug).get("facets"), load_config())
    if not any_found:
        raise Refused("It has no location filter Gus can use, and reading its whole list would be too many requests")
    found, pages = {}, 0
    _cut_short.discard(slug)
    for name, ids in filters.items():  # one filter at a time, as two together would mean "both"
        seen = total = 0
        while True:
            if pages >= WORKDAY_LIST_PAGES:
                _cut_short.add(slug)
                return found
            time.sleep(PAGE_PAUSE)
            pages += 1
            d = workday_jobs(slug, {name: ids}, seen)
            total, rows = total or d.get("total") or 0, d.get("jobPostings") or []
            seen += len(rows)
            for j in rows:
                url = f"https://{host}.myworkdayjobs.com/{site}{j.get('externalPath', '')}"
                if hit := workday_job(url):
                    found[hit[0]] = (url, j.get("title") or hit[1])
            if not rows or seen >= total:
                break
    return found


def workday_job_data(url: str) -> str:
    """The data behind a job's public address."""
    return re.sub(r"^(https://([^.]+)\.[^/]+/)", r"\1wday/cxs/\2/", url)


# ats -> (id prefix, list every job, where a job's details are, what they say)
SITE_BOARDS = {
    "successfactors": ("sf", successfactors_list, lambda url: url, successfactors_page),
    "workday": ("wd", workday_list, workday_job_data, workday_page),
}


def listed_jobs(ats: str, slug: str) -> dict:
    """{id: (address, words to test)} for a board: every job on SuccessFactors, the jobs in your locations on Workday."""
    prefix, list_jobs = SITE_BOARDS[ats][:2]
    return {f"{prefix}:{slug}:{i}": hit for i, hit in list_jobs(slug).items()}


def board_size(ats: str, slug: str) -> int:
    """Open jobs on a board, for checking it exists. One request on Workday."""
    return workday_jobs(slug).get("total", 0) if ats == "workday" else len(listed_jobs(ats, slug))


def job_page(url: str, read_page):
    """What a job page says, GONE if the site says the ad no longer exists, or None if the page made no sense."""
    try:
        text = polite_fetch(url)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return GONE
        raise
    row = read_page(text)
    if not row and BOT_CHECK.search(text):
        raise Refused("A job page answered with a bot check")
    return row


def read_site(company: str, ats: str, slug: str) -> list:
    """Titles come from the board's list. A job's details are read only for wanted titles, once per address, so a big board stays cheap.
    An ad the site says has gone is never asked for again. A page that made no sense is tried again after a week, as it may be a
    one-off, and while several are unreadable and none readable the board is reported as broken on every scan."""
    cfg, known, out = load_config(), known_jobs(), []
    read = readable = unread = 0
    today = date.today()
    listed = listed_jobs(ats, slug)
    _listed.update(listed)
    for jid, (url, words) in listed.items():
        if not wanted_title(words, cfg):
            continue
        looked = looked_up().get(jid)
        if looked and looked["updated"] != url:
            looked = None
        if jid in known:  # already saved: only needs to count as still live
            out.append(dict(known[jid]))
            continue
        if looked and looked.get("unread"):
            if date.fromisoformat(looked["unread"]) + timedelta(days=RETRY_UNREAD_DAYS) > today:
                unread += 1
                continue
        elif looked and (looked.get("gone") or not matches(looked, cfg)):
            readable += not looked.get("gone")
            continue
        if read >= PAGES_PER_SCAN:
            continue
        time.sleep(PAGE_PAUSE if read else 0)
        read += 1
        row = job_page(SITE_BOARDS[ats][2](url), SITE_BOARDS[ats][3])
        if row == GONE:
            looked_up()[jid] = {"updated": url, "gone": True}
        elif not row:
            unread += 1
            looked_up()[jid] = {"updated": url, "unread": str(today)}
        else:
            readable += 1
            looked_up()[jid] = {"updated": url, "title": row["title"], "location": row["location"]}
            out.append({"id": jid, "company": company, "url": url, **row})
    if unread >= UNREAD_TO_FLAG and not readable:
        raise Refused(f"None of its job pages could be read ({unread} tried)")
    return out


def successfactors(company: str, slug: str):
    return read_site(company, "successfactors", slug)


def workday(company: str, slug: str):
    """The list only holds jobs in places the board's filter matched, and a saved role can sit outside it: say, saved for a
    second location. So a saved role that's missing is checked on its own page, and only "gone" lets it close."""
    out = read_site(company, "workday", slug)
    missing = [j for jid, j in known_jobs().items() if jid.startswith(f"wd:{slug}:") and jid not in _listed and not j.get("ad_closed")]
    for n, j in enumerate(missing):
        if n < SAVED_CHECKS:
            time.sleep(PAGE_PAUSE)
            if job_page(workday_job_data(j["url"]), workday_page) == GONE:
                continue
        _listed.add(j["id"])
    return out


def teamtailor_items(slug: str) -> list:
    """Every open job on a board, from the feed each Teamtailor site publishes and lists in its api-catalog. The slug is the careers
    host, as many are a company's own domain and the rest carry a number no one could guess. A board with no jobs gives an empty list."""
    text = polite_fetch(f"https://{slug}/jobs.json")
    try:
        feed = json.loads(text)
    except ValueError:
        feed = None
    if not isinstance(feed, dict) or "jsonfeed.org" not in str(feed.get("version")) or not isinstance(feed.get("items"), list):
        raise Refused("A bot check came back instead of its job feed" if BOT_CHECK.search(text) else "It has no Teamtailor job feed")
    return feed["items"]


def teamtailor_place(job: dict) -> str:
    """Every place in a job's details, then "Remote" if it's remote. The country a remote job names isn't used: it's often just the
    company's home, as on a London job that says Sweden."""
    found = job.get("jobLocation") or []
    places = []
    for p in found if isinstance(found, list) else [found]:
        a = p.get("address") or {}
        places.append(", ".join(dict.fromkeys(filter(None, [a.get("addressLocality"), COUNTRIES.get(a.get("addressCountry"), a.get("addressCountry"))]))))
    return " / ".join(dict.fromkeys(filter(None, places + ["Remote" if job.get("jobLocationType") == "TELECOMMUTE" else ""])))


def teamtailor(company: str, slug: str):
    items = teamtailor_items(slug)
    out = []
    _cut_short.discard(slug)
    for i in items:
        if not (i.get("id") and i.get("title") and i.get("url")):  # one odd ad is skipped, but nothing can be called closed this scan
            _cut_short.add(slug)
            continue
        job = i.get("_jobposting") or {}
        out.append({
            "id": f"tt:{slug}:{i['id']}",
            "company": company,
            "title": html.unescape(i["title"]).strip(),
            "location": teamtailor_place(job),
            "url": i["url"],
            "posted": i.get("date_published") or job.get("datePosted"),
            "description": strip_html(i.get("content_html") or job.get("description")),
        })
    if len(out) >= UNREAD_TO_FLAG and not any(j["location"] for j in out):  # a job can have no place, but not all of them: the feed has changed
        raise Refused("Its feed no longer says where jobs are")
    return out


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
           "smartrecruiters": smartrecruiters, "recruitee": recruitee, "successfactors": successfactors, "workday": workday,
           "teamtailor": teamtailor}


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
            elif ats in PREFIX and slug not in _cut_short:
                answered.add(f"{PREFIX[ats]}:{slug}")
    _boards_answered.clear()
    _boards_answered.update(answered)
    save_looked_up(answered)
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
