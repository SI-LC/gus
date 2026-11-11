"""Social sources: posts from people announcing target roles become signals.

Bluesky and Mastodon have open public search (no key). Reddit needs a free
"script" app (client id + secret in Settings). X lives in radar/x.py (paid API).
LinkedIn and Threads don't offer keyword search to third parties.

Only posts that mention a target role AND one of your locations are kept, so
the agent reads signal rather than the whole firehose.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request

from radar.matching import load_config
from radar.paths import SIGNALS
from radar.web import get_json, strip_html, ua
from radar.settings import secret
from radar.store import locked

BOT = re.compile(r"jobs?\b|alert|careers|hiring\.|recruit|vacanc|\.app$|board", re.I)
TEMPLATE = re.compile(r"new (remote )?job alert|position:|apply (now|here)|#jobsearch", re.I)


def lines(v):
    return [x.strip() for x in re.split(r"[\n,]", v or "") if x.strip()]


def bluesky(queries):
    for q in queries:
        d = get_json("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?" +
                     urllib.parse.urlencode({"q": q, "limit": 50, "sort": "latest"}))
        for p in d.get("posts", []):
            a = p["author"]
            yield {
                "id": f"bsky:{p['uri']}", "source": "bluesky", "name": a.get("displayName") or a["handle"],
                "handle": a["handle"], "text": p["record"].get("text", ""),
                "url": f"https://bsky.app/profile/{a['handle']}/post/{p['uri'].rsplit('/', 1)[-1]}",
                "date": p["record"].get("createdAt"),
            }


def mastodon(tags, instance="mastodon.social"):
    for tag in tags:
        for p in get_json(f"https://{instance}/api/v1/timelines/tag/{urllib.parse.quote(tag.lstrip('#'))}?limit=40"):
            a = p["account"]
            yield {
                "id": f"mastodon:{p['uri']}", "source": "mastodon", "name": a.get("display_name") or a["acct"],
                "handle": a["acct"], "text": strip_html(p.get("content", "")), "url": p["url"], "date": p.get("created_at"),
            }


def reddit(subs):
    cid, csec = secret("reddit_client_id"), secret("reddit_client_secret")
    if not (cid and csec):
        return
    auth = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    req = urllib.request.Request("https://www.reddit.com/api/v1/access_token", data=b"grant_type=client_credentials",
                                 headers={**ua(), "Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r)["access_token"]
    for sub in subs:
        req = urllib.request.Request(f"https://oauth.reddit.com/r/{urllib.parse.quote(sub)}/new?limit=50",
                                     headers={**ua(), "Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            for c in json.load(r)["data"]["children"]:
                p = c["data"]
                yield {
                    "id": f"reddit:{p['id']}", "source": "reddit", "name": p.get("author"), "handle": p.get("author"),
                    "text": f"{p.get('title', '')}\n{p.get('selftext', '')}"[:2000],
                    "url": "https://www.reddit.com" + p["permalink"], "date": p.get("created_utc"),
                }


def main():
    cfg = load_config()
    loc = cfg["location_include"]
    role = cfg["social_role_include"] if cfg["social_role_include"].pattern != "(?!)" else cfg["title_include"]
    runs = []
    if cfg.get("source_bluesky", True):
        runs.append(("Bluesky", lambda: bluesky(lines(cfg.get("social_queries")))))
    if cfg.get("source_mastodon", True):
        runs.append(("Mastodon", lambda: mastodon(lines(cfg.get("social_mastodon_tags")), cfg.get("mastodon_instance") or "mastodon.social")))
    if cfg.get("source_reddit", False):
        runs.append(("Reddit", lambda: reddit(lines(cfg.get("social_reddit_subs")))))

    found, errors = [], []
    for name, fn in runs:
        try:
            found += [p for p in fn() if role.search(p["text"]) and loc.search(p["text"])]
        except Exception as e:
            errors.append(f"{name}: {e}")

    keep_bots = cfg.get("social_include_bots", False)
    added = bots = 0
    with locked(SIGNALS) as signals:
        for p in found:
            if p["id"] in signals:
                continue
            is_bot = bool(BOT.search(f"{p['handle']} {p['name']}") or TEMPLATE.search(p["text"]))
            bots += is_bot
            if is_bot and not keep_bots:
                continue
            signals[p["id"]] = {**p, "kind": "social-bot" if is_bot else "social", "status": "new"}
            added += 1
    print(f"social: {added} new signals ({bots} of {len(found)} matching posts were job bots"
          f"{', kept' if keep_bots else ', skipped'})" + "".join(f"\n  ! {e}" for e in errors))


if __name__ == "__main__":
    main()
