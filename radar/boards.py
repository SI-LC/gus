"""Every job board Gus can read, each as a function that takes (company, slug) and returns that board's open roles as
plain dicts: id, company, title, location, url, posted, description. `SOURCES` maps the `ats` column of the watchlist to its reader.

Three kinds. Job board systems with a public API (Greenhouse, Ashby, Lever, Workable, SmartRecruiters, Recruitee) answer in one
or a few requests. Careers sites with no API (SuccessFactors, Workday, Mind the Product, Getro) give a list of job addresses,
and `read_site` opens a page only for a wanted title. Teamtailor, Gem and GitHub publish one list, and the Google and Amazon
saved searches are read once a day. Everything on a company's own site goes through `web.polite_fetch`.

A scan also needs to know which roles have gone, so readers note every live id in `listed_ids`, and add a board to `cut_short`
when its list didn't finish and a missing role proves nothing.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse

from radar import jobs as J
from radar import matching, web
from radar.paths import DATA, JOBS
from radar.store import read
from radar.web import Refused

LOOKED_UP = DATA / "board_locations.json"
_known = None
_looked_up = None
listed_ids: set = set()  # every live job id seen this scan, wanted or not
cut_short: set = set()  # boards whose list didn't finish, so a missing role proves nothing
# Boards that list every job, so a missing role really has gone. SmartRecruiters is left out: only its first 100 come back.
PREFIX = {"greenhouse": "gh", "ashby": "ashby", "lever": "lever", "workable": "workable", "recruitee": "recruitee",
          "successfactors": "sf", "workday": "wd", "teamtailor": "tt", "gem": "gem", "googlecareers": "gc", "amazonjobs": "amz", "githubcareers": "ghc",
          "mindtheproduct": "mtp", "getro": "getro"}


def known_jobs() -> dict:
    """Roles already saved. Read once per scan."""
    global _known
    if _known is None:
        _known = read(JOBS)
    return _known


def looked_up() -> dict:
    """Where Greenhouse roles we've already fetched are based. Read once per scan. A role edited since is fetched again."""
    global _looked_up
    if _looked_up is None:
        _looked_up = read(LOOKED_UP)
    return _looked_up


def save_looked_up(answered: set) -> None:
    """Forget roles that have gone from a board. Boards that didn't answer this time keep theirs."""
    keep = {k: v for k, v in looked_up().items() if ":".join(k.split(":")[:2]) not in answered or k in listed_ids}
    LOOKED_UP.write_text(json.dumps(keep))


# --- job board systems with a public API ---------------------------------------------------------------

def greenhouse(company: str, slug: str):
    """Big boards list hundreds of jobs. Get titles first, then full details only for new target roles."""
    cfg = matching.load_config()
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    known = known_jobs()
    out = []
    for j in web.get_json(base).get("jobs", []):
        title = j["title"]
        listed_ids.add(f"gh:{slug}:{j['id']}")  # every live job, even titles the filter drops, so nothing looks closed by mistake
        if not matching.wanted_title(title, cfg):
            continue
        jid = f"gh:{slug}:{j['id']}"
        row = {"id": jid, "company": company, "title": title, "url": j["absolute_url"],
               "posted": j.get("first_published") or j.get("updated_at"), "description": "",
               "location": j.get("location", {}).get("name", "")}
        looked = looked_up().get(jid)
        if jid in known:  # already saved: only needs to count as still live
            row["location"] = known[jid].get("location") or row["location"]
        elif looked and looked["updated"] == j.get("updated_at") and not matching.matches({**row, "location": looked["location"]}, cfg):
            continue  # a role elsewhere is never saved, so without this its details would be fetched again every scan
        else:
            d = web.get_json(f"{base}/{j['id']}")
            # some boards (e.g. Cloudflare) put "Hybrid" in location and the cities in offices
            row["location"] = " / ".join(dict.fromkeys(filter(None, [row["location"]] + [o.get("name", "") for o in d.get("offices") or []])))
            row["description"] = web.strip_html(d.get("content", ""))
            looked_up()[jid] = {"updated": j.get("updated_at"), "location": row["location"]}
        out.append(row)
    return out


def ashby(company: str, slug: str):
    d = web.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
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
    d = web.get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
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
    d = web.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
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
    """Every page of the board: it gives 100 jobs at a time, and a big company lists several hundred."""
    posts = []
    for page in range(SR_PAGES):
        time.sleep(PAGE_PAUSE if page else 0)
        d = web.get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={SR_PAGE}&offset={page * SR_PAGE}")
        found = d.get("content") or []
        posts += found
        if len(found) < SR_PAGE or len(posts) >= (d.get("totalFound") or 0):
            break
    out = []
    for j in posts:
        loc = j.get("location", {})
        out.append({
            "id": f"sr:{slug}:{j['id']}",
            "company": company,
            "title": j["name"],
            "location": ", ".join(filter(None, [loc.get("city"), loc.get("country")])) + (" / Remote" if loc.get("remote") else ""),
            "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
            "posted": j.get("releasedDate"),
            "description": "",
            "ref": j.get("refNumber") or None,  # the vacancy's number: a re-post gets a new id and keeps this
        })
    return out


def recruitee(company: str, slug: str):
    d = web.get_json(f"https://{slug}.recruitee.com/api/offers/")
    return [
        {
            "id": f"recruitee:{slug}:{j['id']}",
            "company": company,
            "title": j["title"],
            "location": j.get("location", ""),
            "url": j["careers_url"],
            "posted": j.get("published_at"),
            "description": web.strip_html(j.get("description", "")),
        }
        for j in d.get("offers", [])
    ]


# --- careers sites: what each site publishes, within its robots.txt -------------------------------------

PAGE_PAUSE = 1.5  # seconds between job pages on one board
SR_PAGE = 100  # jobs per request, the most SmartRecruiters gives
SR_PAGES = 20  # requests per board, so 2,000 jobs
PAGES_PER_SCAN = 25  # job pages read per board; the rest wait for the next scan
RETRY_UNREAD_DAYS = 7  # a job page that made no sense is tried again after this long
UNREAD_TO_FLAG = 3  # this many unreadable pages, and none readable, means the board itself is the problem
GONE = "gone"  # the site said the ad no longer exists
WORKDAY_LIST_PAGES = 50  # of 20 jobs each, so 1,000 jobs in your locations; a longer list is cut short
SAVED_CHECKS = 10  # saved roles missing from a Workday list whose own page is checked each scan
GOOGLE_SEARCH = "https://www.google.com/about/careers/applications/jobs/results"
GOOGLE_EVERY = 20 * 3600  # seconds between reads: a little under a day, so a daily run at a set time is never skipped
GOOGLE_NAMES = {"DeepMind": "Google DeepMind"}
AMAZON_HOST = "https://www.amazon.jobs"
AMAZON_PAGE = 100  # jobs per request, the most the site gives
AMAZON_PAGES = 5  # requests per read, so 500 roles; a longer list is cut short
GITHUB_HOST = "https://www.github.careers"
GITHUB_PAGE = 100  # jobs per request, the most the site gives
GITHUB_PAGES = 10  # requests per read, so 1,000 roles; a longer list is cut short
GITHUB_PAUSE = 5  # seconds between pages: the crawl-delay its robots.txt asks for
MTP_HOST = "www.mindtheproduct.com"
BIG_BOARD_PAGES = 100  # job pages read per scan on a board of many companies: thousands of ads, hundreds with wanted titles
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
COUNTRIES = {"GB": "United Kingdom", "IE": "Ireland", "US": "United States", "FR": "France", "DE": "Germany", "NL": "Netherlands",
             "ES": "Spain", "SE": "Sweden", "DK": "Denmark", "NO": "Norway", "FI": "Finland"}  # Teamtailor gives codes; others stay as codes



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
    return {"title": d.get("title", ""), "posted": d.get("startDate"), "description": web.strip_html(d.get("jobDescription", "")),
            "location": " / ".join(filter(None, [d.get("location"), *(d.get("additionalLocations") or [])]))}


def successfactors_list(slug: str) -> dict:
    """{job id: (address, words to test)} from the sitemap. The slug is the careers host."""
    found = {}
    for url in web.sitemap_urls(f"https://{slug}/sitemap.xml"):
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
    return json.loads(web.polite_fetch(f"{workday_data(slug)}/jobs", json.dumps(ask).encode()))


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
    filters, any_found = location_filters(workday_jobs(slug).get("facets"), matching.load_config())
    if not any_found:
        raise Refused("It has no location filter Gus can use, and reading its whole list would be too many requests")
    found, pages = {}, 0
    cut_short.discard(slug)
    for name, ids in filters.items():  # one filter at a time, as two together would mean "both"
        seen = total = 0
        while True:
            if pages >= WORKDAY_LIST_PAGES:
                cut_short.add(slug)
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


def employer_ad(url) -> str:
    """The employer's own ad behind a job site's listing, without the apply-form ending some boards add (Lever's /apply or
    /app, Ashby's /application), so it's the same address Gus has for that role from the company's own board. "" if none."""
    url = str(url or "").strip()
    return re.sub(r"/(?:apply|app|application)/?$", "", url) if url.startswith("https://") else ""


def mtp_job(url: str):
    """(job id, words to test) from an address like /jobs/listing/group-product-manager-b704f3b80c7aeaaf/. The words are the
    title, sometimes with the place, and the id is the code the site puts at the end."""
    m = re.fullmatch(r"/jobs/listing/([a-z0-9-]+)-([a-z0-9]+)/?", urlparse(url).path)
    return (m[2], m[1].replace("-", " ")) if m and urlparse(url).netloc == MTP_HOST else None


def mtp_list(slug: str) -> dict:
    """{job id: (address, words to test)} from the jobs sitemap. The board's own list and filters run through /api/, which its
    robots.txt rules out, and its page shows only the first 20 jobs whatever is asked: the sitemap is the one full list."""
    found = {}
    for url in web.sitemap_urls(f"https://{MTP_HOST}/sitemap-jobs.xml"):
        if hit := mtp_job(url):
            found[hit[0]] = (url, hit[1])
    if not found:  # an empty list is an error, or every saved role would look closed
        raise Refused("Its sitemap lists no jobs")
    return found


def mtp_page(text: str):
    """Company, title, place, date and description from the data in a job page, GONE once the ad has expired, or None if the
    page holds no job. The link is the employer's own ad when the board gives one. Anyone can post an ad here, so what an
    ad says is only ever data."""
    m = NEXT_DATA.search(text)
    try:
        j = json.loads(m[1])["props"]["pageProps"]["job"]
        company, title = j["company"].strip(), html.unescape(j["title"]).strip()
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    if not company or not title:
        return None
    if (j.get("expiresAt") or "9") < datetime.now(timezone.utc).isoformat():
        return GONE
    place = j.get("location") or ""
    if j.get("country") and j["country"].lower() not in place.lower():
        place = ", ".join(filter(None, (place, j["country"])))
    extra = [x for x in (j.get("requirements") or []) + (j.get("benefits") or []) if isinstance(x, str)]
    row = {"company": company, "title": title, "location": place, "posted": j.get("publishedAt") or None,
           "description": web.strip_html("\n".join([j.get("description") or "", *extra]))}
    if ad := employer_ad(j.get("applicationUrl")):  # the employer's ad, so a role also on its own board is one role
        row["url"] = ad
    return row


def getro_job(url: str):
    """(job id, words to test) from an address like /companies/acme/jobs/95290675-senior-product-manager. The words are the title
    as the board shortened it, and some ads have none: those can't be told apart from any other job, so they're never read."""
    m = re.fullmatch(r"/companies/[^/]+/jobs/(\d+)(?:-([a-z0-9-]+))?/?", urlparse(url).path)
    return (m[1], (m[2] or "").replace("-", " ")) if m else None


def getro_list(slug: str) -> dict:
    """{job id: (address, words to test)} from a Getro board's sitemap. The slug is the board's host. Its page shows the first
    20 jobs and the rest come from Getro's API, so the sitemap, which lists every job, is the polite way to the whole list."""
    found = {}
    for url in web.sitemap_urls(f"https://{slug}/sitemap.xml"):
        if urlparse(url).netloc == slug and (hit := getro_job(url)):
            found[hit[0]] = (url, hit[1])
    if not found:
        raise Refused("Its sitemap lists no jobs: it may not be a Getro board")
    return found


def getro_page(text: str):
    """Company, title, places, date and text from the data in a Getro job page, GONE once the job has closed, or None if the
    page holds no job. The link is the employer's own ad when the board gives one, so a role also on its own board is one role."""
    m = NEXT_DATA.search(text)
    try:
        j = json.loads(m[1])["props"]["pageProps"]["initialState"]["jobs"]["currentJob"]
        company, title = j["organization"]["name"].strip(), html.unescape(j["title"]).strip()
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    if not company or not title:
        return None
    if j.get("status", "active") != "active" or j.get("closedAt") or j.get("deactivatedAt"):
        return GONE
    row = {"company": company, "title": title, "posted": j.get("postedAt") or None, "description": web.strip_html(j.get("description") or ""),
           "location": " / ".join(dict.fromkeys(p["name"] for p in j.get("locations") or [] if isinstance(p, dict) and p.get("name")))}
    if ad := employer_ad(j.get("url")):
        row["url"] = ad
    return row


# ats -> (id prefix, list every job, where a job's details are, what they say)
SITE_BOARDS = {
    "successfactors": ("sf", successfactors_list, lambda url: url, successfactors_page),
    "workday": ("wd", workday_list, workday_job_data, workday_page),
    "mindtheproduct": ("mtp", mtp_list, lambda url: url, mtp_page),
    "getro": ("getro", getro_list, lambda url: url, getro_page),
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
        text = web.polite_fetch(url)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return GONE
        raise
    row = read_page(text)
    if not row and web.BOT_CHECK.search(text):
        raise Refused("A job page answered with a bot check")
    return row


def excluded(row: dict, cfg) -> bool:
    """A role whose page names a company on the leave-out list: recruiters and job sites, as for the other job sources."""
    return bool(row.get("company")) and matching.left_out(row["company"], cfg, "source_company_exclude")


def read_site(company: str, ats: str, slug: str) -> list:
    """Titles come from the board's list. A job's details are read only for wanted titles, once per address, so a big board stays cheap.
    An ad the site says has gone is never asked for again. A page that made no sense is tried again after a week, as it may be a
    one-off, and while several are unreadable and none readable the board is reported as broken on every scan.
    On a board of many companies (Mind the Product) each page names its company, and one on your leave-out list is skipped."""
    cfg, known, out = matching.load_config(), known_jobs(), []
    alias = J.aliases(known)
    pages_read = readable = unread = 0
    today = date.today()
    listed = listed_jobs(ats, slug)
    listed_ids.update(listed)
    for jid, (url, words) in listed.items():
        if not matching.wanted_title(words, cfg):
            continue
        looked = looked_up().get(jid)
        if looked and looked["updated"] != url:
            looked = None
        if jid in alias:  # already saved, maybe as a sighting of the same ad elsewhere: only needs to count as still live
            out.append({**known[alias[jid]], "id": jid, "url": url} if alias[jid] != jid else dict(known[jid]))
            continue
        if looked and looked.get("unread"):
            if date.fromisoformat(looked["unread"]) + timedelta(days=RETRY_UNREAD_DAYS) > today:
                unread += 1
                continue
        elif looked and (looked.get("gone") or not matching.matches(looked, cfg) or excluded(looked, cfg)):
            readable += not looked.get("gone")
            continue
        if pages_read >= (BIG_BOARD_PAGES if ats in ("mindtheproduct", "getro") else PAGES_PER_SCAN):
            continue
        time.sleep(PAGE_PAUSE if pages_read else 0)
        pages_read += 1
        row = job_page(SITE_BOARDS[ats][2](url), SITE_BOARDS[ats][3])
        if row == GONE:
            looked_up()[jid] = {"updated": url, "gone": True}
        elif not row:
            unread += 1
            looked_up()[jid] = {"updated": url, "unread": str(today)}
        else:
            readable += 1
            looked_up()[jid] = {"updated": url, "title": row["title"], "location": row["location"],
                                **({"company": row["company"]} if row.get("company") else {})}
            if not excluded(row, cfg):
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
    missing = [j for jid, j in known_jobs().items() if jid.startswith(f"wd:{slug}:") and jid not in listed_ids and not j.get("ad_closed")]
    for n, j in enumerate(missing):
        if n < SAVED_CHECKS:
            time.sleep(PAGE_PAUSE)
            if job_page(workday_job_data(j["url"]), workday_page) == GONE:
                continue
        listed_ids.add(j["id"])
    return out


def mind_the_product(company: str, slug: str):
    """Mind the Product's jobs board: many companies' ads, listed in full in its sitemap. Each role carries the company its ad
    names, so it rolls up to that account, and the same ad found on the company's own board is kept as one role."""
    return read_site(company, "mindtheproduct", slug)


def getro(company: str, slug: str):
    """A VC's portfolio job board run on Getro: the companies it backs, each role under its own company."""
    return read_site(company, "getro", slug)


def getro_hosts(text: str) -> list:
    """The boards in Settings, one address per line, as hosts. Anything that isn't a public web address is left out."""
    hosts = [urlparse(line if "//" in line else f"https://{line}").netloc.lower() for line in (text or "").split() if line.strip()]
    return list(dict.fromkeys(h for h in hosts if web.PUBLIC_HOST.fullmatch(h)))


def teamtailor_items(slug: str) -> list:
    """Every open job on a board, from the feed each Teamtailor site publishes and lists in its api-catalog. The slug is the careers
    host, as many are a company's own domain and the rest carry a number no one could guess. A board with no jobs gives an empty list."""
    text = web.polite_fetch(f"https://{slug}/jobs.json")
    try:
        feed = json.loads(text)
    except ValueError:
        feed = None
    if not isinstance(feed, dict) or "jsonfeed.org" not in str(feed.get("version")) or not isinstance(feed.get("items"), list):
        raise Refused("A bot check came back instead of its job feed" if web.BOT_CHECK.search(text) else "It has no Teamtailor job feed")
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
    cut_short.discard(slug)
    for i in items:
        if not (i.get("id") and i.get("title") and i.get("url")):  # one odd ad is skipped, but nothing can be called closed this scan
            cut_short.add(slug)
            continue
        job = i.get("_jobposting") or {}
        out.append({
            "id": f"tt:{slug}:{i['id']}",
            "company": company,
            "title": html.unescape(i["title"]).strip(),
            "location": teamtailor_place(job),
            "url": i["url"],
            "posted": i.get("date_published") or job.get("datePosted"),
            "description": web.strip_html(i.get("content_html") or job.get("description")),
        })
    if len(out) >= UNREAD_TO_FLAG and not any(j["location"] for j in out):  # a job can have no place, but not all of them: the feed has changed
        raise Refused("Its feed no longer says where jobs are")
    return out


def gem_posts(slug: str) -> list:
    """Every open job on a board, in one reply, from the public job board API Gem documents for custom careers pages: no key, no
    pages. The slug is the path in jobs.gem.com/<slug>. A board with no jobs gives an empty list; an unknown board gives a 404."""
    text = web.polite_fetch(f"https://{web.GEM_API}/job_board/v0/{slug}/job_posts/")
    try:
        posts = json.loads(text)
    except ValueError:
        posts = None
    if not isinstance(posts, list):
        raise Refused("A bot check came back instead of its job list" if web.BOT_CHECK.search(text) else "It has no Gem job list")
    return posts


def gem_place(job: dict) -> str:
    """Every office, by the name candidates see, with the place behind it when that adds something: "London (United Kingdom - Remote)".
    The top-level location is only the first office. location_type can't rule remote out, as it says hybrid on "Poland - Remote" jobs,
    so "Remote" is added only when it says remote and no place does: a place that names a country keeps that limit."""
    places = []
    for office in job.get("offices") or []:
        name, place = office.get("name") or "", (office.get("location") or {}).get("name") or ""
        adds_nothing = set(re.findall(r"\w+", re.sub(r"\(.*?\)", "", name).lower())) <= set(re.findall(r"\w+", place.lower()))
        places.append(place if adds_nothing else f"{name} ({place})" if place else name)
    found = " / ".join(dict.fromkeys(filter(None, places))) or (job.get("location") or {}).get("name") or ""
    return " / ".join(filter(None, [found, "Remote" if job.get("location_type") == "remote" and "remote" not in found.lower() else ""]))


def gem_job(company: str, slug: str, post: dict) -> dict:
    title = " ".join(html.unescape(post["title"]).split())
    if not (title and post["id"] and post["absolute_url"]):
        raise ValueError("Part of the ad is empty")
    return {
        "id": f"gem:{slug}:{post['id']}",
        "company": company,
        "title": title,
        "location": gem_place(post),
        "url": post["absolute_url"],
        "posted": post.get("first_published_at"),
        "description": web.strip_html(post.get("content_plain") or post.get("content")),
    }


def gem(company: str, slug: str):
    posts = gem_posts(slug)
    out = []
    cut_short.discard(slug)
    for post in posts:
        try:
            out.append(gem_job(company, slug, post))
        except (AttributeError, KeyError, TypeError, ValueError):  # one odd ad is skipped, but nothing can be called closed this scan
            cut_short.add(slug)
    if len(posts) >= UNREAD_TO_FLAG and not any(j["location"] for j in out):  # a job can have no place, but not all of them: the list has changed
        raise Refused("Its job list no longer says where jobs are")
    return out


def google_address(text: str) -> str:
    """The search to read, always newest first and never past page one, which is all robots.txt allows for Google's job results."""
    parts = urlparse(text.strip())
    if f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/") != GOOGLE_SEARCH:
        raise Refused("This isn't a search on Google's careers site")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("page", "sort_by")]
    return f"{GOOGLE_SEARCH}?{urlencode(query + [('sort_by', 'date')], quote_via=quote)}"


def google_results(text: str) -> tuple:
    """(rows, total) from the data in a Google Careers page, which is the only place it says what matched. Google can change this
    format at any time, so anything unexpected is reported rather than read as "no jobs"."""
    for block in re.findall(r"AF_initDataCallback\(\{key: 'ds:\d+'.*?data:(.*?), sideChannel:", text, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, list) and len(data) == 4 and isinstance(data[2], int) and isinstance(data[3], int):
            return data[0] or [], data[2]
    raise Refused("A bot check came back instead of its jobs" if web.BOT_CHECK.search(text) else "The page no longer has job results Gus can read")


def google_job(row: list, links: dict) -> dict:
    def text(n):
        return (row[n] or [None, ""])[1] or ""
    stamp = (row[14] or row[12])[0]
    return {
        "id": f"gc:search:{row[0]}",
        "company": GOOGLE_NAMES.get(row[7], row[7]),
        "title": html.unescape(row[1]).strip(),
        "location": " / ".join(dict.fromkeys(p[0] for p in row[9] or [])),
        "url": f"{GOOGLE_SEARCH}/{links.get(row[0], row[0])}",
        "posted": datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds"),
        "description": web.strip_html("\n".join((text(10), text(3), text(4)))),
    }


def read_google(address: str) -> list:
    page = web.polite_fetch(address)
    rows, total = google_results(page)
    links = {link.split("-")[0]: link for link in re.findall(r"jobs/results/(\d+-[^?\"\\&]+)", page)}
    try:
        jobs = [google_job(r, links) for r in rows]
    except (IndexError, KeyError, TypeError, ValueError) as e:
        raise Refused("The page no longer has job results Gus can read") from e
    if len(jobs) >= UNREAD_TO_FLAG and not any(j["location"] for j in jobs):
        raise Refused("The page no longer says where jobs are")
    if total > len(jobs):  # only the first page can be read, so a role missing from it may just have moved down
        cut_short.add("search")
    return jobs


def read_daily(slug: str, address: str, state_file, reader) -> list:
    """A saved search read at most once a day, whatever else runs. Between reads nothing is asked for, and a read that failed
    stays on the Health page until the next one."""
    cut_short.add(slug)  # nothing can be called closed unless this read finishes
    state = read(state_file)
    if state.get("address") == address and time.time() - state.get("read", 0) < GOOGLE_EVERY:
        if state.get("error"):
            raise Refused(state["error"])
        return []
    state = {"address": address, "read": time.time()}
    try:
        cut_short.discard(slug)
        return reader(address)
    except Exception as e:
        cut_short.add(slug)
        state["error"] = str(e)[:200]
        raise
    finally:
        state_file.write_text(json.dumps(state))


def google_careers(company: str, slug: str):
    """One search, one page a day: all robots.txt allows for Google's job results."""
    address = google_address(matching.load_config().get("google_careers_search") or "")
    return read_daily(slug, address, DATA / "google_careers.json", read_google)


def amazon_address(text: str) -> str:
    """The search to read on amazon.jobs: the one you pasted, as its JSON form, newest first. Location and keywords stay as you set
    them on the site, so nothing about where you're looking is decided here."""
    parts = urlparse(text.strip())
    m = re.fullmatch(r"/([a-z]{2}(?:-[a-z]{2})?)/search(?:\.json)?/?", parts.path)
    if f"{parts.scheme}://{parts.netloc}" != AMAZON_HOST or not m:
        raise Refused("This isn't a search on amazon.jobs")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("offset", "result_limit", "sort")]
    return f"{AMAZON_HOST}/{m[1]}/search.json?{urlencode(query + [('sort', 'recent')])}"  # spaces as +, the site's own form


def amazon_job(j: dict) -> dict:
    posted = None
    try:
        posted = datetime.strptime(re.sub(r"\s+", " ", j.get("posted_date") or ""), "%B %d, %Y").date().isoformat()
    except ValueError:
        pass
    return {
        "id": f"amz:search:{j['id_icims']}",
        "company": "Amazon",
        "title": html.unescape(j["title"]).strip(),
        "location": j.get("normalized_location") or j.get("location") or "",
        "url": f"{AMAZON_HOST}{j['job_path']}",
        "posted": posted,
        "description": web.strip_html("\n".join(filter(None, (j.get("description"), j.get("basic_qualifications"), j.get("preferred_qualifications"))))),
    }


def read_amazon(address: str) -> list:
    """Every page of the search, newest first, until a page comes back short or the page limit is reached."""
    jobs = []
    for page in range(AMAZON_PAGES):
        try:
            d = json.loads(web.polite_fetch(f"{address}&result_limit={AMAZON_PAGE}&offset={page * AMAZON_PAGE}"))
            found = [amazon_job(j) for j in d["jobs"]]
        except (ValueError, KeyError, TypeError) as e:
            raise Refused("The site no longer answers with job results Gus can read") from e
        jobs += found
        if len(found) < AMAZON_PAGE:
            return jobs
        time.sleep(PAGE_PAUSE)
    cut_short.add("amazon")  # more pages than Gus reads: a role missing from these may just be further down
    return jobs


def amazon_jobs(company: str, slug: str):
    """Amazon's own careers site, which isn't one of the job board systems: a saved search, read once a day."""
    address = amazon_address(matching.load_config().get("amazon_jobs_search") or "")
    return read_daily(slug, address, DATA / "amazon_jobs.json", read_amazon)


def github_job(company: str, slug: str, j: dict) -> dict:
    """One ad from the list on github.careers: the ad text is in three parts, which the job page shows one after another."""
    place, country = j.get("location_name") or "", j.get("full_location") or ""
    return {
        "id": f"ghc:{slug}:{j['req_id']}",
        "company": company,
        "title": html.unescape(j["title"]).strip(),
        "location": place if country in place else " / ".join(filter(None, (place, country))),
        "url": f"{GITHUB_HOST}/careers-home/jobs/{j['slug']}",
        "posted": j.get("posted_date") or None,
        "description": web.strip_html("\n".join(filter(None, (j.get("description"), j.get("responsibilities"), j.get("qualifications"))))),
    }


def read_github(company: str, slug: str) -> list:
    """Every page of the list, until a page comes back short or the page limit is reached. The site asks for five seconds between
    requests, so a long list takes a while: this is why it's read once a day, like the Amazon and Google searches."""
    jobs = []
    for page in range(1, GITHUB_PAGES + 1):
        if page > 1:
            time.sleep(GITHUB_PAUSE)
        try:
            d = json.loads(web.polite_fetch(f"{GITHUB_HOST}/api/jobs?page={page}&limit={GITHUB_PAGE}"))
            found = [github_job(company, slug, j["data"]) for j in d["jobs"]]
        except (ValueError, KeyError, TypeError) as e:
            raise Refused("The site no longer answers with job results Gus can read") from e
        jobs += found
        if len(found) < GITHUB_PAGE:
            return jobs
    cut_short.add(slug)  # more pages than Gus reads: a role missing from these may just be further down
    return jobs


def github_count() -> int:
    """How many roles github.careers lists, for the resolver: the smallest page carries the total."""
    return int(json.loads(web.polite_fetch(f"{GITHUB_HOST}/api/jobs?page=1&limit=1")).get("totalCount") or 0)


def github_careers(company: str, slug: str):
    """GitHub hires on its own site, github.careers, which isn't one of the job board systems. There is one list for the whole
    company, so the slug is only its name in `watchlist.csv`; the list is read once a day."""
    return read_daily(slug, GITHUB_HOST, DATA / "github_careers.json", lambda address: read_github(company, slug))


def hn_who_is_hiring():
    """Latest 'Ask HN: Who is hiring?' thread: top-level comments, each filtered by matching.matches()."""
    s = web.get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring")
    story = next(h for h in s["hits"] if h["title"].lower().startswith("ask hn: who is hiring"))
    thread = web.get_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    out = []
    for c in thread.get("children", []):
        text = web.strip_html(c.get("text") or "")
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
           "teamtailor": teamtailor, "gem": gem, "googlecareers": google_careers, "amazonjobs": amazon_jobs,
           "githubcareers": github_careers, "mindtheproduct": mind_the_product,
           "getro": getro}
