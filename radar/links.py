"""Point roles from job search sources at the employer's own ad, so you don't land on a job site's login.

    python3 -m radar.links     # check roles in your inbox that haven't been checked yet (run after each sources job)

Public job boards are free to read, so this uses no AI and none of your Adzuna or JSearch allowance. A link only changes
when the same title is found on the company's board. Otherwise the role keeps its link and the cockpit offers a web search.

With "Find employer pages with AI" on in Settings, strong roles still without a link get a Claude web search. That uses
your Claude allowance, so it's limited per run and stops at your daily AI limit.
"""
from __future__ import annotations

import csv
import json
import re
import urllib.error
from datetime import date
from urllib.parse import urlparse

from radar import ai
from radar.resolve import resolve
from radar.scan import JOBS, ROOT, SOURCES, fetch_bytes, load_config, watchlist_path
from radar.sources import LABEL, same_company, same_title
from radar.store import locked

MIN_SCORE = 5  # the same line as the Role Inbox: anything you might open
PER_RUN = 40  # each company tries several boards and names, so keep a run polite


JOB_SITES = re.compile(r"(^|\.)(linkedin|adzuna|indeed|glassdoor|totaljobs|reed|jobleads|jooble|bebee|jobijoba|simplyhired|talent|jobrapido|cv-library|"
                       r"monster|ziprecruiter|justlondonjobs|efinancialcareers|otta|welcometothejungle|builtin|google|bing)\.", re.I)
NO_TOOLS = ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]  # web pages can hold instructions, so it can only search and read the web


def usable(url) -> bool:
    """Only a real web address that isn't a job site, and doesn't answer 'gone'. A site blocking the check still counts."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or JOB_SITES.search(parsed.hostname) or parsed.path in ("", "/"):
        return False
    try:
        fetch_bytes(str(url), timeout=20)
    except urllib.error.HTTPError as e:
        return e.code not in (404, 410)
    except Exception:
        pass
    return True


def ai_find(job):
    """The employer's page from a Claude web search, or None."""
    about = {k: job.get(k) for k in ("company", "title", "location")} | {"description": (job.get("description") or "")[:800]}
    prompt = (ROOT / "prompts" / "find_link.md").read_text().replace("{{JOB}}", json.dumps(about, ensure_ascii=False))
    text = ai.run(prompt, task="links", tools=["WebSearch", "WebFetch"], deny=NO_TOOLS, timeout=300)
    try:
        url = json.loads(text[text.index("{"): text.rindex("}") + 1]).get("url")
    except (ValueError, AttributeError):
        return None
    return url if usable(url) else None


def ai_waiting(store, cfg) -> list:
    return [j for j in store.values() if j.get("source") in LABEL and (j.get("score") or 0) >= int(cfg.get("links_ai_min_score", 7))
            and j.get("link_checked") and j.get("link_kind") != "company" and not j.get("ai_link_checked")
            and j.get("stage") != "skipped" and not j.get("ad_closed")][:int(cfg.get("links_ai_per_run", 5))]


def run_ai(cfg) -> None:
    done = {}
    for j in ai_waiting(json.loads(JOBS.read_text()) if JOBS.exists() else {}, cfg):
        try:
            done[j["id"]] = ai_find(j)
        except (ai.BudgetReached, ai.NeedsTools) as e:
            print(f"AI link search stopped: {e}")
            break
        except Exception as e:  # tried again next time
            print(f"{j['company']}: {type(e).__name__}")
    with locked(JOBS) as store:
        for jid, url in done.items():
            j = store.get(jid)
            if not j:
                continue
            j["ai_link_checked"] = str(date.today())
            if url:
                j.update(source_url=j["url"], url=url, link_kind="company")
    print(f"AI searched {len(done)} roles, found {sum(bool(u) for u in done.values())} employer pages")


def find(company, title, boards):
    """The employer's own ad for this title, or None. `boards` remembers each company's board for this run."""
    name = same_company(company)
    if name not in boards:
        boards[name] = resolve(name) if name else None
    if not boards[name]:
        return None
    ats, slug, _ = boards[name]
    wanted = same_title(title)
    for row in SOURCES[ats](company, slug):
        if same_title(row["title"]) == wanted and row.get("url"):
            return {"url": row["url"], "ats": ats, "slug": slug}
    return None


def waiting(store) -> list:
    return [j for j in store.values() if j.get("source") in LABEL and (j.get("score") or 0) >= MIN_SCORE
            and j.get("stage") != "skipped" and not j.get("ad_closed") and not j.get("link_checked")][:PER_RUN]


def watched() -> set:
    return {same_company(r["company"]) for r in csv.DictReader(watchlist_path().open())}


def watch(job) -> bool:
    """Add the role's company to the watchlist, using the board already confirmed by finding this role on it."""
    board = job.get("board") or {}
    if not board or same_company(job["company"]) in watched():
        return False
    with watchlist_path().open("a", newline="") as f:
        csv.writer(f).writerow([job["company"], board["ats"], board["slug"], "discovered"])
    return True


def main():
    todo = waiting(json.loads(JOBS.read_text()) if JOBS.exists() else {})
    boards, found = {}, {}
    for j in todo:  # network outside the lock
        try:
            found[j["id"]] = find(j["company"], j["title"], boards)
        except Exception as e:  # one bad board shouldn't stop the rest; this role is tried again next time
            print(f"{j['company']}: {type(e).__name__}")
            found[j["id"]] = False
    with locked(JOBS) as store:
        for jid, hit in found.items():
            j = store.get(jid)
            if not j or hit is False:
                continue
            j["link_checked"] = str(date.today())
            if hit:
                j.update(source_url=j["url"], url=hit["url"], link_kind="company", board={"ats": hit["ats"], "slug": hit["slug"]})
    print(f"Checked {len(found)} roles, found {sum(bool(h) for h in found.values())} on company sites")
    cfg = load_config()
    if cfg.get("links_ai_search"):
        run_ai(cfg)


if __name__ == "__main__":
    main()
