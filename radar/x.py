"""Search X for people announcing PM roles in London/UK, via the official X API.

Needs your own bearer token from developer.x.com, set in the cockpit's Settings
(stored in .secrets.json, git-ignored) or the X_BEARER_TOKEN env var. X bills per post returned, so this
only asks for posts newer than the last run and stops at `x_daily_post_cap`.

Each post becomes a signal in data/signals.json: the agent turns signals into
account events and people to contact.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date

from radar import usage
from radar.scan import DATA, ROOT, load_config

SIGNALS = DATA / "signals.json"
STATE = DATA / "x_state.json"

QUERY = ('("hiring" OR "we\'re hiring" OR "my team is hiring" OR "join my team") '
         '("product manager" OR "product lead" OR "head of product" OR "PM role") '
         '(London OR UK OR "United Kingdom") -is:retweet -is:reply lang:en')


def token():
    from radar.settings import secret
    return secret("x_bearer_token")


def main():
    if not load_config().get("source_x", True):
        print("X: turned off in settings")
        return
    t = token()
    if not t:
        print("X: no token set (Settings → Keys); skipping")
        return
    cap = load_config().get("x_daily_post_cap", 150)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if state.get("day") != str(date.today()):
        state.update(day=str(date.today()), used=0)
    if state["used"] >= cap:
        print(f"X: daily cap of {cap} posts reached")
        return

    params = {
        "query": QUERY,
        "max_results": str(max(10, min(100, cap - state["used"]))),
        "tweet.fields": "created_at,author_id,entities",
        "expansions": "author_id",
        "user.fields": "name,username,description,url",
    }
    if state.get("since_id"):
        params["since_id"] = state["since_id"]
    req = urllib.request.Request(
        "https://api.x.com/2/tweets/search/recent?" + urllib.parse.urlencode(params),
        headers={"Authorization": f"Bearer {t}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)

    posts = d.get("data", [])
    users = {u["id"]: u for u in d.get("includes", {}).get("users", [])}
    signals = json.loads(SIGNALS.read_text()) if SIGNALS.exists() else {}
    for p in posts:
        u = users.get(p["author_id"], {})
        signals[f"x:{p['id']}"] = {
            "source": "x",
            "name": u.get("name"),
            "handle": u.get("username"),
            "bio": u.get("description", ""),
            "text": p["text"],
            "links": [e.get("expanded_url") for e in p.get("entities", {}).get("urls", [])],
            "url": f"https://x.com/{u.get('username', 'i')}/status/{p['id']}",
            "date": p.get("created_at"),
            "status": "new",
        }
    state["used"] += len(posts)
    if posts:
        cfg = load_config()
        usage.record(kind="x", posts=len(posts), cost_usd=round(len(posts) * float(cfg.get("x_cost_per_post") or 0), 4))
    if d.get("meta", {}).get("newest_id"):
        state["since_id"] = d["meta"]["newest_id"]
    SIGNALS.write_text(json.dumps(signals, indent=2, ensure_ascii=False))
    STATE.write_text(json.dumps(state))
    print(f"X: {len(posts)} new posts ({state['used']}/{cap} today)")


if __name__ == "__main__":
    main()
