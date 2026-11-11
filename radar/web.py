"""How Gus asks other people's sites for anything: as a guest.

Every request says who is asking, waits its turn for the host, and is limited in size and time. `fetch_bytes` is for public
APIs and feeds. `polite_fetch` is for a company's own site: it also follows the site's robots.txt, and `Refused` is what
comes back when a site says no, which is reported and never worked around.
"""
from __future__ import annotations

import html
import http.client
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urlparse

from radar.paths import ROOT

REPO = "https://github.com/SI-LC/gus"
VERSION_KEEP = 3600  # seconds before the release tag is read again
_version = (0.0, "")  # (good until, release)


def version() -> str:
    """The release this code is from, read from its git tag, so a site operator seeing Gus in their logs knows which one asked.
    "dev" when there is no tag or no git. Read again every hour, because the release is tagged after the Pi has started the
    new code: the service picks the tag up without a restart."""
    global _version
    if _version[0] > time.monotonic():
        return _version[1]
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    _version = (time.monotonic() + VERSION_KEEP, out.lstrip("v") or "dev")
    return _version[1]


def ua() -> dict:
    """Who is asking: the convention is name/version and, in brackets, a link where the site's owner can read what the bot is
    and reach whoever runs it. A bot that can be identified and is plainly small is the kind nobody bothers to block."""
    return {"User-Agent": f"gus/{version()} (+{REPO}; a personal job-search radar)", "Accept-Encoding": "gzip"}


HOST_PAUSE = 1.5  # seconds between any two requests to one host, whichever threads send them
_hosts: dict = {}  # host -> the moment it may next be asked
_hosts_lock = threading.Lock()


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


def _pace(host: str) -> None:
    """Requests to one host come at least HOST_PAUSE apart, whichever thread sends them. The scan reads six boards at once, and
    many of them live on the same vendor's host: paced, they arrive one at a time rather than as a burst from one address."""
    with _hosts_lock:
        now = time.monotonic()
        turn = max(now, _hosts.get(host, 0))
        _hosts[host] = turn + HOST_PAUSE
    if turn > now:
        time.sleep(turn - now)


def fetch_bytes(url: str, timeout: int = 30, body: bytes = None, headers: dict = None) -> bytes:
    """Web sources only, with a size limit, so a bad feed or a redirect can't reach local files or fill memory."""
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError(f"Not a web address: {url[:60]}")
    req = urllib.request.Request(url, data=body, headers={**ua(), **(headers or {})})
    _pace(urlparse(url).netloc.lower())
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


def get_json(url: str):
    return json.loads(fetch_bytes(url))


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


# --- a company's own site: only what its robots.txt allows -------------------------------------------

class Refused(ValueError):
    """A site said no: a robots.txt rule, a refused request or a bot check. Reported for the board, never worked around."""


ROBOTS_KEEP = 3600  # seconds to trust a robots.txt
BOT_CHECK = re.compile(r"captcha|just a moment|cf-chl|attention required|access denied", re.I)
GEM_API = "api.gem.com"
PUBLIC_HOST = re.compile(r"(?:[a-z0-9-]+\.)+(?!local$|internal$|lan$|home$)[a-z]{2,}", re.I)  # no IP addresses, ports or home network names
WORKDAY_JSON = {"Accept": "application/json", "Content-Type": "application/json"}
_robots: dict = {}


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
        # Gem's API has no robots.txt: its gateway gives this 403 for every address it doesn't know. The job list still answers for itself.
        no_file = e.code == 403 and host == GEM_API and e.headers.get("x-amzn-ErrorType") == "ForbiddenException"
        if (e.code in (401, 403, 429) or e.code >= 500) and not no_file:
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
