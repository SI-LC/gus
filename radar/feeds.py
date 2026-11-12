"""Pull the RSS/Atom feeds you chose (Settings, Newsletters) into data/signals.json.

Newsletters, funding news, job posts and community threads all become signals; the
discovery agent turns them into account events, roles and people.
"Signals" feeds are pre-filtered by keyword so the agent only reads relevant items.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from radar.matching import load_config
from radar.paths import DATA, SIGNALS
from radar.web import fetch_bytes, strip_html
from radar.store import read

FULL_TEXT = 15_000  # job lists name dozens of roles, so they keep far more than the teaser
SIGNAL_WORDS = re.compile(r"rais|fund|series [a-e]|seed|expan|launch|hiring|acqui|ai\b|agent", re.I)
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


def parse_xml(data: bytes):
    """Feeds never need entity declarations, and they're how XML bombs work."""
    if b"<!ENTITY" in data or b"<!DOCTYPE" in data[:2000]:
        raise ValueError("Feed declares XML entities; refused")
    return ET.fromstring(data)


def with_links(body: str) -> str:
    """Keep where each link goes, since a job list's apply links are most of its value."""
    return re.sub(r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", r" \2 (\1) ", body, flags=re.I | re.S)


def fetch(url: str, full: bool = False):
    root = parse_xml(fetch_bytes(url))
    items = root.findall(".//item") or root.findall(f".//{ATOM}entry")
    for it in items:
        get = lambda tag, it=it: (it.findtext(tag) or it.findtext(ATOM + tag) or "").strip()
        link = get("link")
        if not link:
            el = it.find(ATOM + "link")
            link = el.get("href", "") if el is not None else ""
        yield {
            "title": get("title"),
            "url": link,
            "date": get("pubDate") or get("published") or get("updated"),
            "text": (full and strip_html(with_links(it.findtext(CONTENT) or get("content")))[:FULL_TEXT])
                    or strip_html(get("description") or get("summary") or get("content"))[:1500],
        }


SUGGESTED = [  # checked to have working feeds; nothing here is read until you add it
    ("Lenny's Newsletter", "https://www.lennysnewsletter.com/feed", "newsletter"),
    ("The Pragmatic Engineer", "https://newsletter.pragmaticengineer.com/feed", "newsletter"),
    ("Product Growth (Aakash Gupta)", "https://www.news.aakashg.com/feed", "newsletter"),
    ("Product Compass", "https://www.productcompass.pm/feed", "newsletter"),
    ("Elena's Growth Scoop", "https://www.elenaverna.com/feed", "newsletter"),
    ("The Beautiful Mess (John Cutler)", "https://cutlefish.substack.com/feed", "newsletter"),
    ("Latent Space", "https://www.latent.space/feed", "newsletter"),
    ("Import AI", "https://importai.substack.com/feed", "newsletter"),
    ("One Useful Thing", "https://www.oneusefulthing.org/feed", "newsletter"),
    ("Exponential View", "https://www.exponentialview.co/feed", "newsletter"),
    ("Nonlinear News", "https://nonlinearnews.com/feed", "newsletter"),
    ("Not Boring", "https://www.notboring.co/feed", "newsletter"),
    ("The Generalist", "https://www.generalist.com/feed", "newsletter"),
    ("Stratechery", "https://stratechery.com/feed/", "newsletter"),
    ("Sifted", "https://sifted.eu/feed", "signals"),
    ("EU-Startups", "https://www.eu-startups.com/feed/", "signals"),
    ("UKTN", "https://www.uktech.news/feed", "signals"),
    ("job hunting sux", "https://www.jobhuntingsux.com/feed", "jobs"),
    ("a16z Jobs", "https://a16zbuild.substack.com/feed", "jobs"),
    ("Ali Rohde Jobs", "https://alirohdejobs.substack.com/feed", "jobs"),
    ("next play", "https://nextplayso.substack.com/feed", "jobs"),
]


def _parses(url):
    try:
        root = parse_xml(fetch_bytes(url, timeout=20))
        title = root.findtext("./channel/title") or root.findtext(f"{ATOM}title") or ""
        if root.findall(".//item") or root.findall(f".//{ATOM}entry"):
            return title.strip() or url
    except Exception:
        return None
    return None


def find_feed(text: str):
    """Turn a Substack name, newsletter URL or feed URL into (title, feed_url).

    Handles Substack ("lenny", "lenny.substack.com", custom domains), Beehiiv, Ghost,
    Medium ("medium.com/@name"), Buttondown, and any page that advertises its feed."""
    t = text.strip().rstrip("/")
    if not t:
        raise ValueError("Paste a newsletter name or link")
    if re.fullmatch(r"[a-z0-9-]+", t, re.I):  # bare Substack name
        t = f"https://{t}.substack.com"
    if not t.startswith("http"):
        t = "https://" + t
    m = re.match(r"https://(www\.)?medium\.com/(@[^/]+)", t)
    if m:
        t = f"https://medium.com/feed/{m.group(2)}"
    m = re.match(r"https://substack\.com/@([^/]+)", t)
    if m:
        raise ValueError("That's a Substack profile. Paste the newsletter's own link (e.g. name.substack.com) instead")
    title = _parses(t)
    if title:
        return title, t
    try:  # the page may advertise its feed
        page = fetch_bytes(t, timeout=20)[:400_000].decode("utf-8", "ignore")
        for tag in re.findall(r"<link[^>]+>", page, re.I):
            if re.search(r"application/(rss|atom)\+xml", tag, re.I):
                href = re.search(r'href=["\']([^"\']+)', tag)
                if href:
                    u = urllib.parse.urljoin(t + "/", html.unescape(href.group(1)))
                    title = _parses(u)
                    if title:
                        return title, u
    except Exception:
        pass
    for suffix in ("/feed", "/rss", "/rss/", "/feed.xml", "/rss.xml", "/atom.xml"):
        title = _parses(t + suffix)
        if title:
            return title, t + suffix
    host = urllib.parse.urlparse(t).netloc
    if not host.startswith("www.") and host.count(".") == 1:  # some sites only serve the feed on www
        try:
            return find_feed(t.replace("://", "://www.", 1))
        except ValueError:
            pass
    raise ValueError("Couldn't find a feed there. Check the link, or paste the feed URL itself")


def add_feed(text: str, kind: str = "newsletter"):
    from radar.settings import load, save
    if kind not in ("newsletter", "signals", "jobs", "community"):
        raise ValueError("Unknown kind")
    title, url = find_feed(text)
    title = re.sub(r"\s*[|]\s*", " - ", title)[:60]
    if any(f["url"].rstrip("/") == url.rstrip("/") for f in all_feeds()):
        raise ValueError(f"{title} is already in your feeds")
    extra = (load().get("feeds_extra") or "").strip()
    every = {"jobs": 10, "signals": 30}.get(kind, 60)
    save({"feeds_extra": (extra + "\n" if extra else "") + f"{title} | {url} | {kind} | {every}"})
    return title, url


def remove_feed(name: str):
    from radar.settings import load, save
    lines = [l for l in (load().get("feeds_extra") or "").splitlines() if l.split("|")[0].strip() != name]
    save({"feeds_extra": "\n".join(lines)})


def is_signal(text: str, places=None) -> bool:
    """News worth an agent's look: funding, launches, hiring and the like, or anything naming one of your own locations."""
    return bool(SIGNAL_WORDS.search(text) or (places and places.search(text)))


def all_feeds():
    """Your feeds, from Settings, each marked on or off. A new install starts with the few in config.json; every one is
    yours to turn off or remove."""
    from radar.settings import load
    cfg = load()
    off = {x.strip().lower() for x in (cfg.get("feeds_disabled") or "").splitlines() if x.strip()}
    rows = []
    for line in (cfg.get("feeds_extra") or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[1].startswith("http"):
            rows.append({"name": parts[0], "url": parts[1], "kind": parts[2] if len(parts) > 2 else "signals",
                         "every_min": parts[3] if len(parts) > 3 else "30"})
    for r in rows:
        r["enabled"] = r["name"].lower() not in off
    return rows


def main():
    state_path = DATA / "feeds_state.json"
    state = read(state_path)
    t = datetime.now(timezone.utc).timestamp()
    feeds = [f for f in all_feeds() if f["enabled"]
             and t - state.get(f["name"], 0) >= int(f.get("every_min") or 10) * 60 - 30]
    signals = read(SIGNALS)
    places = load_config().get("location_include")
    errors_path = DATA / "feed_errors.json"
    errors = read(errors_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def run(f):
        try:
            return f, list(fetch(f["url"], full=f["kind"] == "jobs")), None
        except Exception as e:
            return f, [], e

    added = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for f, items, err in pool.map(run, feeds):
            if err:
                print(f"  ! {f['name']}: {err}")
                errors[f["name"]] = str(err)
                continue
            errors.pop(f["name"], None)
            state[f["name"]] = t
            for it in items:
                key = f"feed:{it['url']}"
                if key in signals or not it["url"]:
                    continue
                if f["kind"] == "signals" and not is_signal(it["title"] + " " + it["text"], places):
                    continue
                signals[key] = {"source": f["name"], "kind": f["kind"], **it, "first_seen": now, "status": "new"}
                added += 1
    SIGNALS.write_text(json.dumps(signals, indent=2, ensure_ascii=False))
    state_path.write_text(json.dumps(state))
    on = {f["name"] for f in all_feeds() if f["enabled"]}  # a feed you removed or turned off is no longer a problem
    errors_path.write_text(json.dumps({n: e for n, e in errors.items() if n in on}))
    print(f"feeds: {added} new signals ({sum(l['status'] == 'new' for l in signals.values())} waiting for the agent)")


if __name__ == "__main__":
    main()
