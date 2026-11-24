"""Map the people who matter at target accounts (prompts/people.md).

    python3 -m radar.people                 # A/B accounts with fresh events and fewer than 2 people mapped
    python3 -m radar.people "Acme"          # one account

Sources: Bluesky's open search here, then Claude with web search. No messages are
drafted at this stage: drafts belong to weekly moves, where there's a signal to anchor them.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from radar import ai, doing
from radar.accounts import brief, person_id
from radar.paths import COMPANIES, PEOPLE
from radar.store import read
from radar.notify import notify
from radar.matching import load_config
from radar.paths import JOBS, ROOT
from radar.web import get_json
from radar.store import locked

PROMPT = (ROOT / "prompts" / "people.md").read_text()
MIN_PEOPLE = 2
RETRY_DAYS = 7


def bluesky(company: str, limit: int = 25):
    q = urllib.parse.quote(f'"{company}" hiring product')
    try:
        d = get_json(f"https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={q}&limit={limit}&sort=latest")
    except Exception:
        return []
    return [
        {
            "name": p["author"].get("displayName") or p["author"]["handle"],
            "handle": p["author"]["handle"],
            "text": p["record"].get("text", "")[:400],
            "url": f"https://bsky.app/profile/{p['author']['handle']}/post/{p['uri'].rsplit('/', 1)[-1]}",
            "date": p["record"].get("createdAt"),
        }
        for p in d.get("posts", [])
    ]


def first_list(text: str) -> list:
    """The first JSON list in the model's reply, ignoring any prose or further JSON around it."""
    dec = json.JSONDecoder()
    i = text.find("[")
    while i != -1:
        try:
            found, _ = dec.raw_decode(text, i)
        except ValueError:
            found = None
        if isinstance(found, list) and all(isinstance(p, dict) for p in found):
            return found
        i = text.find("[", i + 1)
    return []


def map_account(name: str) -> list:
    c = read(COMPANIES, {}).get(name, {})
    jobs = [{k: j[k] for k in ("title", "url", "location", "score")} | {"description": j.get("description", "")[:3000]}
            for j in read(JOBS, {}).values() if j.get("company") == name and (j.get("score") or 0) >= 6]
    known = [{k: p.get(k) for k in ("name", "role", "kind")} for p in read(PEOPLE, {}).values() if p.get("company") == name]
    account = {"name": name, **{k: c.get(k) for k in ("product", "london", "stage", "product_leaders", "careers_url", "fit_why")}}
    prompt = (PROMPT.replace("{{PROFILE}}", (ROOT / "profile" / "profile.md").read_text())
              .replace("{{ACCOUNT}}", json.dumps(account, ensure_ascii=False))
              .replace("{{JOBS}}", json.dumps(jobs[:3], ensure_ascii=False))
              .replace("{{KNOWN}}", json.dumps(known, ensure_ascii=False))
              .replace("{{BLUESKY}}", json.dumps(bluesky(name) if load_config().get("source_bluesky", True) else [], ensure_ascii=False)))
    text = ai.run(prompt, task="people", tools=["WebSearch", "WebFetch"])
    found = first_list(text)
    added = []
    with locked(PEOPLE) as people:
        for p in found:
            if not (p.get("name") and p.get("url") and p.get("evidence")):
                continue
            pid = person_id(p["name"], name)
            rec = people.setdefault(pid, {"company": name, "stage": "mapped", "added": str(date.today()), "warm": False})
            for k in ("name", "role", "kind", "platform", "url", "evidence", "evidence_date", "confidence", "past_companies"):
                if p.get(k) and not (k == "url" and rec.get("warm")):
                    rec[k] = p[k]
            rec["source"] = "people search"
            added.append(p["name"])
    return added


AT_ONCE = 2  # accounts searched at the same time when several are asked for: each is a Claude of its own, and the Pi is small
MOST = 8  # and the most taken from one call, so it's done in a few minutes


def asked_for(args: list[str]) -> list[str]:
    """The companies named on the command line: one each, or one name typed without quotes ("Arva AI")."""
    whole = " ".join(args)
    return [whole] if len(args) > 1 and whole in read(COMPANIES, {}) else list(args)


def main():
    if len(sys.argv) > 1:
        targets = asked_for(sys.argv[1:])[:MOST]
    else:
        companies = read(COMPANIES, {})
        recent = str(date.today() - timedelta(days=RETRY_DAYS))
        targets = [a["account"] for a in brief()["accounts"] if len(a["people"]) < MIN_PEOPLE
                   and (companies.get(a["account"], {}).get("people_checked") or "") < recent][:4]  # a few per run keeps each scan quick
    try:
        ai.check_budget(load_config())  # before anything is marked as checked
    except ai.BudgetReached as e:
        print(f"  people mapping skipped: {e}")
        return
    with locked(COMPANIES) as companies:  # hard accounts wait a week, rather than being searched every run
        for name in targets:
            companies.setdefault(name, {})["people_checked"] = str(date.today())

    def one(name):
        try:
            return name, map_account(name)
        except ai.NeedsTools:
            raise
        except Exception as e:
            return name, e
    doing.say(f"Mapping people at {', '.join(targets)}")
    try:
        with ThreadPoolExecutor(max_workers=AT_ONCE if len(sys.argv) > 1 else 1) as pool:  # a scan maps its few one after another, as before
            results = list(pool.map(one, targets))
    except ai.NeedsTools as e:
        sys.exit(f"people mapping skipped: {e}")
    for name, added in results:
        if isinstance(added, Exception):
            print(f"  ! {name}: {added}")
            continue
        print(f"  {name}: {', '.join(added) or 'no one with evidence'}")
        if added:
            notify(f"Gus found {len(added)} {'person' if len(added) == 1 else 'people'} at {name}", "", ", ".join(added[:3]) + (f" and {len(added) - 3} more" if len(added) > 3 else ""))


if __name__ == "__main__":
    main()
