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
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from radar.paths import DATA, ROOT
from radar.store import locked, read

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


HOST_PAUSE = 1.5  # seconds between any two requests to one vendor, whichever threads send them
SLOWER_PAUSE = 10  # the same for a vendor that has said "slow down", for CAREFUL_DAYS after: no vendor says what gap it wants
# The same, always, for a vendor that has turned Gus away at the usual pace, in seconds, with what was seen:
# - Workday: one board takes many requests and every company shares one front door. Seven boards 1.5 seconds apart was some
#   80 requests in two minutes, and it put up a bot check.
# - Workable: answered 429 on and off for two weeks at 1.5 seconds; all twelve boards read at 10.
# - Microsoft: answers two requests 10 seconds apart and refuses the third, with no wait given. A minute is a guess.
GENTLE = {"myworkdayjobs.com": 30, "workable.com": 10, "microsoft.com": 60}
_hosts: dict = {}  # vendor -> the moment it may next be asked
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


def vendor(host: str) -> str:
    """Who really answers for a host: acme.wd3.myworkdayjobs.com and bolt.wd5.myworkdayjobs.com are both myworkdayjobs.com.
    Every company on one vendor shares its front door, which sees Gus as a single visitor, so pacing and "slow down" are
    counted per vendor."""
    host = host.lower().rsplit(":", 1)[0] if host.count(":") == 1 else host.lower()
    if re.fullmatch(r"[\d.]+|\[.*\]", host):  # an address, not a name: it is its own vendor
        return host
    parts = host.split(".")
    two_part_ending = len(parts) > 2 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "ac", "gov", "net")
    return ".".join(parts[-3:] if two_part_ending else parts[-2:])


def _pace(who: str) -> None:
    """Requests to one vendor come at least HOST_PAUSE apart, whichever thread sends them. The scan reads six boards at once,
    and many of them live with the same vendor: paced, they arrive one at a time rather than as a burst from one address."""
    gap = pause(who)
    with _hosts_lock:
        now = time.monotonic()
        turn = max(now, _hosts.get(who, 0))
        _hosts[who] = turn + gap
    if turn > now:
        time.sleep(turn - now)


# --- when a vendor says "slow down": ask less, then stop asking for a while ------------------------------

class Refused(ValueError):
    """A site said no: a robots.txt rule, a refused request or a bot check. Reported for the board, never worked around."""


class SlowDown(Refused):
    """Not asked at all: the vendor told Gus to slow down, and Gus is waiting before it asks again."""


SLOW_DOWN = DATA / "slow_down.json"  # {vendor: {"until", "times", "said", "why", "asked_for"}}, shared by every Gus process
SHORT_WAIT = 30  # seconds: a "try again in" this short is waited out once, there and then
STRIKES = 3  # refusals in a row from one vendor, with no answer between them, before Gus changes what it does
COOL_OFF = 60  # seconds the vendor is left alone the first time that happens in a run
FIRST_HOLD = 2 * 3600  # seconds it isn't asked at all when it still says no after that: long enough to cover one run
LONGEST_HOLD = 20 * 3600  # each hold in a row is twice the last, up to this: under a day, so the daily run always asks once
BOT_CHECK_HOLD = 7 * 86400 - 4 * 3600  # a vendor that answers with a bot check again is asked once a week: waiting hours doesn't cure one, and asking daily may keep it in place. Just under a week, so the scan seven days on asks
CAREFUL_DAYS = 7  # how long after a "slow down" the vendor is still asked at SLOWER_PAUSE, though it answers again
_strikes: dict = {}  # vendor -> refusals in a row, in this process
_cooled: set = set()  # vendors already given their cool-off in this process


def bot_check(e: urllib.error.HTTPError) -> bool:
    """The check a vendor's front door puts up for a visitor it takes for a bot. It can come with any code, a 429 included."""
    return (e.headers or {}).get("cf-mitigated") == "challenge"


def told_to_slow_down(e: urllib.error.HTTPError) -> bool:
    """A 429, or the bot check a vendor's front door puts up once it has had enough of a visitor."""
    return e.code == 429 or (e.code in (403, 503) and bot_check(e))


def said_slow_down(text: str) -> bool:
    """Whether a board's recorded failure was the vendor saying slow down, rather than the board having moved or broken."""
    return bool(re.search(r"\b429\b|slow down|too many requests", text or "", re.I))


def why(e: urllib.error.HTTPError) -> str:
    """What the vendor said, in a few words: slowing down can cure "too many requests", and never a bot check."""
    return f"a bot check ({e.code})" if e.code != 429 or bot_check(e) else "too many requests (429)"


def held_back(failure: str) -> bool:
    """Whether a board's recorded failure is one that waiting cures: its vendor was on hold, or answered 429. Those boards
    are read again the same day, once the hold runs out (`hold_ended`). A bot check isn't one: it's left to the next daily read."""
    return bool(re.search(r"Not asked: |\b429\b", failure or "")) and "bot check" not in (failure or "")


def _retry_after(e: urllib.error.HTTPError):
    try:
        return max(0, int((e.headers or {}).get("Retry-After")))
    except (TypeError, ValueError):
        return None


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _holds() -> dict:
    return read(SLOW_DOWN) if SLOW_DOWN.exists() else {}


def _stale(rec: dict) -> bool:
    """Said so long ago that the vendor is asked at the usual pace again."""
    return rec.get("said", "") < (_utc() - timedelta(days=CAREFUL_DAYS)).isoformat(timespec="seconds")


def _said_lately(who: str) -> bool:
    rec = _holds().get(who)
    return bool(rec) and not _stale(rec)


def hold_ended() -> str:
    """When the latest hold to run out did so, while its vendor hasn't answered since; "" when none is waiting to be asked.
    The service reads the boards held back once for each such moment."""
    now = _utc().isoformat(timespec="seconds")
    return max((r["until"] for r in _holds().values() if r.get("times") and r.get("until", now) < now), default="")


def pause(who: str) -> float:
    """Seconds between two requests to this vendor: longer for one that has said slow down lately, or is always asked gently."""
    return max(GENTLE.get(who, HOST_PAUSE), SLOWER_PAUSE if who in _cooled or _said_lately(who) else 0)


def unhurried(who: str) -> bool:
    """Whether this vendor is asked more slowly than most. The scan reads its boards in a slot of their own (`scan.fetch_all`)."""
    return pause(who) > HOST_PAUSE


def _check_hold(who: str) -> None:
    rec = _holds().get(who)
    if rec and rec.get("until", "") > _utc().isoformat(timespec="seconds"):
        again = datetime.fromisoformat(rec["until"]).astimezone().strftime("%H:%M on %-d %b")
        said = rec.get("why") or ""
        what = f"answered with {said}" if "bot check" in said else "told Gus to slow down" + (f" ({said})" if said else "")
        raise SlowDown(f"Not asked: {who} {what}. Gus asks it again after {again}")


def _refused(who: str, wait, said: str = "") -> None:
    """One more "slow down" from a vendor. STRIKES in a row and it's left alone for COOL_OFF, then asked more slowly; if it
    still says no, Gus stops asking it until the hold runs out. A vendor held before this run, and not heard from since,
    gets that one slower try at its first refusal: it has already said no, and one more no shouldn't cost another day.
    A bot check is different: waiting doesn't cure it, so a vendor that answers with one again is held for BOT_CHECK_HOLD at once."""
    with _hosts_lock:
        _strikes[who] = _strikes.get(who, 0) + 1
        held_before = bool((_holds().get(who) or {}).get("times"))
        if _strikes[who] < STRIKES and not held_before:
            return
        _strikes[who] = 0
        again = "bot check" in said and held_before  # a bot check from a vendor held before: no second try, one request is the week's
        if who not in _cooled and not again:
            _cooled.add(who)
            _hosts[who] = max(_hosts.get(who, 0), time.monotonic() + COOL_OFF)
            return
    with locked(SLOW_DOWN) as holds:
        rec, now = holds.get(who) or {}, _utc()
        if rec.get("until", "") > now.isoformat(timespec="seconds"):
            return  # another thread or process has just put it on hold
        times = rec.get("times", 0) + 1
        hold = BOT_CHECK_HOLD if "bot check" in said and times > 1 else min(LONGEST_HOLD, max(FIRST_HOLD * 2 ** (times - 1), wait or 0))
        holds[who] = {"until": (now + timedelta(seconds=hold)).isoformat(timespec="seconds"), "times": times,
                      "said": now.isoformat(timespec="seconds"), "why": said, "asked_for": wait}


def _answered(who: str) -> None:
    """The vendor answered: the count starts again and its hold is over, so the next one would start short. What it said is
    kept for CAREFUL_DAYS, to go on asking it slowly, and then forgotten."""
    with _hosts_lock:
        _strikes.pop(who, None)
    rec = _holds().get(who)
    if rec and (rec.get("times") or _stale(rec)):
        with locked(SLOW_DOWN) as holds:
            if _stale(holds.get(who) or {}):
                holds.pop(who, None)
            else:
                holds[who] = {k: v for k, v in holds[who].items() if k != "until"} | {"times": 0}


def _fetch(req, timeout: int) -> bytes:
    if getattr(_limit, "deadline", None) is not None:
        return _fetch_by(req, _limit.deadline)
    for attempt in range(3):  # wifi connections drop now and then; a 4xx won't change, so don't retry those
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(MAX_RESPONSE + 1)
                if r.headers.get("Content-Encoding") == "gzip":
                    d = zlib.decompressobj(16 + zlib.MAX_WBITS)  # bounded, so a tiny download can't expand without limit
                    data = d.decompress(data, MAX_RESPONSE + 1)
            return data
        except urllib.error.HTTPError:
            raise
        except (OSError, http.client.HTTPException):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_bytes(url: str, timeout: int = 30, body: bytes = None, headers: dict = None) -> bytes:
    """Web sources only, with a size limit, so a bad feed or a redirect can't reach local files or fill memory. A vendor that
    says "slow down" is given the short wait it asks for once; beyond that the refusal counts against it (`_refused`)."""
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError(f"Not a web address: {url[:60]}")
    req = urllib.request.Request(url, data=body, headers={**ua(), **(headers or {})})
    who = vendor(urlparse(url).netloc)
    _check_hold(who)
    _pace(who)
    try:
        data = _fetch(req, timeout)
    except urllib.error.HTTPError as e:
        if not told_to_slow_down(e):
            raise
        wait = _retry_after(e)
        if bot_check(e):
            e.msg = "A bot check"  # so the board's failure says what it was, whatever code came with it
        if wait is None or wait > SHORT_WAIT or getattr(_limit, "deadline", None) is not None:
            _refused(who, wait, why(e))
            raise
        time.sleep(wait)
        try:
            data = _fetch(req, timeout)
        except urllib.error.HTTPError as again:
            if told_to_slow_down(again):
                _refused(who, _retry_after(again), why(again))
            if bot_check(again):
                again.msg = "A bot check"
            raise
    _answered(who)
    if len(data) > MAX_RESPONSE:
        raise ValueError(f"Response too large: {url[:60]}")
    return data


def get_json(url: str):
    return json.loads(fetch_bytes(url))


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


# --- a company's own site: only what its robots.txt allows -------------------------------------------

ROBOTS_KEEP = 24 * 3600  # seconds to trust a robots.txt: a day, the longest RFC 9309 allows, so a daily scan seldom asks for one
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
    if kept and kept[0] > time.time():
        return kept[1]
    try:
        rules = parse_robots(fetch_bytes(f"https://{host}/robots.txt").decode("utf-8-sig", "ignore"))
    except urllib.error.HTTPError as e:
        # Gem's API has no robots.txt: its gateway gives this 403 for every address it doesn't know. The job list still answers for itself.
        no_file = e.code == 403 and host == GEM_API and e.headers.get("x-amzn-ErrorType") == "ForbiddenException"
        if (e.code in (401, 403, 429) or e.code >= 500) and not no_file:
            raise Refused(f"robots.txt answered {'with ' + why(e) if bot_check(e) else e.code}") from e
        rules = ([], [])
    _robots[host] = (time.time() + ROBOTS_KEEP, rules)
    return rules


def remember_robots(path) -> None:
    """Start a scan with the robots.txt files the last one read, where they're under a day old. The file is
    {host: [the moment it stops being trusted, [rules, sitemaps]]}."""
    kept = read(path)
    _robots.update({host: (until, (rules, sitemaps)) for host, (until, (rules, sitemaps)) in kept.items() if until > time.time()})


def keep_robots(path) -> None:
    """Save the robots.txt files read, for the next scan. One that couldn't be read is never kept: it is asked for again."""
    with locked(path) as kept:
        kept.clear()
        kept.update({host: [until, list(rules)] for host, (until, rules) in _robots.items() if until > time.time()})


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
