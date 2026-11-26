"""Point roles from job search sources at the employer's own ad, so you don't land on a job site's login, and read the
employer's board once a role Gus kept has been found on it.

    python3 -m radar.links          # check roles that haven't been checked yet (run after each sources job and in the full run)
    python3 -m radar.links <id>     # look for this role's employer ad now, board then web search ("Find employer's ad")

Every role that didn't come from a board scan is checked, whatever it scored, so each gets the employer's own link when
there is one. The employer itself only arrives when Gus kept the role (scored 5 or more, not skipped): its board joins the
watchlist, so its real ads are read at source, and the company waits as a `candidate`, in no tier, until the watchlist agent
has researched and scored it. An employer whose role Gus skipped isn't added. Public job boards
are free to read, so this uses no AI and none of your Adzuna or JSearch allowance. A link only changes when the same
title is found on the company's board, and an Adzuna or JSearch role found there becomes a board role. Otherwise the role
keeps its link and the cockpit offers a web search.

With "Find employer pages with AI" on in Settings, strong roles still without a link get a Claude web search. That uses
your Claude allowance, so it's limited per run and stops at your daily AI limit. A role the search misses is tried once
more a week later: employers often post on their own board a few days after a job site has the ad.

The employer's ad also says where the role really is and carries the whole advert, where a job site may file an ad from
abroad under "UK" and cut the text short. So a found ad gives the role its location and text as well as its link, and the
role is scored again: one that turns out to be abroad drops out of Role Inbox the way any low score does.
"""
from __future__ import annotations

import csv
import json
import re
import urllib.error
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from radar import ai
from radar.companies import COMPANIES
from radar.resolve import GUESS_DAYS, from_careers, resolve
from radar.boards import SOURCES
from radar.matching import load_config
from radar.paths import watchlist_path, DATA, JOBS, ROOT
from radar.web import fetch_bytes
from radar.jobs import JOB_SITES as SITE_SOURCES, from_employer, location_unconfirmed, same_company, same_title, to_board
from radar.sources import LABEL
from radar.store import locked, read

LOOKUP_SOURCES = set(LABEL) | {"web", "email", "x"}  # found somewhere other than the employer's board
GUESSES = DATA / "board_guesses.json"  # {company: {"checked", "board", "careers"}}: what looking for its board turned up
PER_RUN = 40  # each company tries several boards and names, so keep a run polite
WATCH_SCORE = 5  # triage skips a role under this, and an employer is only worth a look when Gus kept its role
AI_TRIES = 2  # web searches a role gets: the second a week after the first
AI_AGAIN_DAYS = 7


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
    """The employer's page from a Claude web search, and where it says the role is: {"url", "location"}, or None."""
    about = {k: job.get(k) for k in ("company", "title", "location")} | {"description": (job.get("description") or "")[:800]}
    prompt = (ROOT / "prompts" / "find_link.md").read_text().replace("{{JOB}}", json.dumps(about, ensure_ascii=False))
    text = ai.run(prompt, task="links", tools=["WebSearch", "WebFetch"], deny=NO_TOOLS, timeout=300)
    try:
        reply = json.loads(text[text.index("{"): text.rindex("}") + 1])
        url, place = reply.get("url"), reply.get("location")
    except (ValueError, AttributeError):
        return None
    return {"url": url, "location": place.strip()[:200] if isinstance(place, str) else None} if usable(url) else None


def ai_due(j) -> bool:
    """Not searched for yet, or searched once at least a week ago."""
    if not j.get("ai_link_checked"):
        return True
    return int(j.get("ai_link_tries") or 1) < AI_TRIES and j["ai_link_checked"] <= str(date.today() - timedelta(days=AI_AGAIN_DAYS))


def ai_waiting(store, cfg) -> list:
    return [j for j in store.values() if j.get("source") in LABEL and (j.get("score") or 0) >= int(cfg.get("links_ai_min_score", 7))
            and j.get("link_checked") and j.get("link_kind") != "company" and ai_due(j)
            and j.get("stage") != "skipped" and not j.get("ad_closed")][:int(cfg.get("links_ai_per_run", 5))]


def on_its_board(job, url):
    """The role as the employer's board lists it, when the page a web search found is on a board Gus can read: that gives the
    whole advert, and the board is then read at source. None when it isn't, or the board doesn't answer."""
    try:
        board = from_careers(url)
        return find(job["company"], job["title"], {same_company(job["company"]): board}) if board else None
    except Exception:
        return None


def save(j, hit, now) -> bool:
    """Point the role at the employer's ad and take the ad's location and text. Returns whether it should be scored again."""
    if hit["url"] != j.get("url"):
        j.setdefault("source_url", j.get("url"))
    j.update(url=hit["url"], link_kind="company")
    if hit.get("ats"):
        j["board"] = {"ats": hit["ats"], "slug": hit["slug"]}
        to_board(j)  # a job site's role, found on the employer's board: from now on it's a board role
        if hit.get("id") and hit["id"] != j["id"]:  # so the board scan sees it, and closes it when the ad goes
            j.setdefault("also", {})[hit["id"]] = {"url": hit["url"], "source": "board", "seen": now}
    return from_employer(j, hit.get("location"), hit.get("description")) and j.get("score") is not None


def run_ai(cfg, only=None) -> list:
    """Web-search for the roles waiting, or for `only` these whatever they scored. Returns the roles to score again."""
    done, again = {}, []
    for j in (only if only is not None else ai_waiting(read(JOBS), cfg)):
        try:
            hit = ai_find(j)
            done[j["id"]] = hit and (on_its_board(j, hit["url"]) or hit)
        except (ai.BudgetReached, ai.NeedsTools) as e:
            print(f"AI link search stopped: {e}")
            break
        except Exception as e:  # tried again next time
            print(f"{j['company']}: {type(e).__name__}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with locked(JOBS) as store:
        for jid, hit in done.items():
            j = store.get(jid)
            if not j:
                continue
            j["ai_link_tries"] = int(j.get("ai_link_tries") or (1 if j.get("ai_link_checked") else 0)) + 1
            j["ai_link_checked"] = str(date.today())
            if hit and save(j, hit, now):
                again.append(jid)
    print(f"AI searched {len(done)} roles, found {sum(bool(u) for u in done.values())} employer pages")
    return again


def careers_addresses() -> dict:
    """Each known company's careers address, by its cleaned-up name: the surest way to its board."""
    companies = read(COMPANIES)
    return {same_company(n): r["careers_url"] for n, r in companies.items() if r.get("careers_url") and not r.get("careers_url_broken")}


def find(company, title, boards, careers=None):
    """The employer's own ad for this title, or None. `boards` remembers each company's board for this run."""
    name = same_company(company)
    if name not in boards:
        boards[name] = resolve(name, (careers or {}).get(name)) if name else None
    if not boards[name]:
        return None
    ats, slug, _ = boards[name]
    wanted = same_title(title)
    for row in SOURCES[ats](company, slug):
        if same_title(row["title"]) == wanted and row.get("url"):
            return {"url": row["url"], "ats": ats, "slug": slug, "id": row.get("id")} | {k: row[k] for k in ("location", "description") if row.get(k)}
    return None


def waiting(store) -> list:
    """Unchecked roles from anywhere but a board scan, best first, so the strong ones are covered within a run's limit."""
    todo = [j for j in store.values() if j.get("source") in LOOKUP_SOURCES and not j.get("ad_closed") and not j.get("link_checked")]
    return sorted(todo, key=lambda j: j.get("score") or 0, reverse=True)[:PER_RUN]


def unfilled(store) -> list:
    """Roles Gus pointed at an employer's board before it took the location and text from there: filled in once, a run's
    worth at a time. Skipped and closed ones are left as they are."""
    return [j for j in store.values() if j.get("board") and j.get("link_kind") == "company" and j.get("found_via") in SITE_SOURCES
            and not j.get("location_from") and not j.get("location_checked") and not j.get("ad_closed")
            and j.get("stage") not in ("skipped", "closed")][:PER_RUN]


def rescore(ids) -> None:
    """Score again the roles whose location or text the employer's ad changed. Each keeps its score if scoring can't run."""
    if not ids:
        return
    from radar import triage
    try:
        triage.main(ids)
    except SystemExit as e:
        print(f"Not scored again yet: {e}")


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


def discover(store) -> list:
    """Employers new to Gus whose role it kept and found on their board. Each board joins the watchlist and the company
    becomes a candidate for the watchlist agent to research: never a watched, tiered account on the strength of one ad.
    A company already on the record is left alone, whatever its status, so one you removed stays removed; so is a board
    already read under another name, which would only be the same company twice."""
    rows = list(csv.DictReader(watchlist_path().open()))
    names = {same_company(n) for n in read(COMPANIES)} | {same_company(r["company"]) for r in rows}
    boards = {(r["ats"], r["slug"]) for r in rows}
    new = {}
    for j in store.values():
        board, name = j.get("board") or {}, same_company(j.get("company"))
        if (not board or not name or (j.get("score") or 0) < WATCH_SCORE or j.get("stage") in ("skipped", "closed") or j.get("ad_closed")
                or name in names or (board["ats"], board["slug"]) in boards):
            continue
        if watch(j):
            names.add(name)
            boards.add((board["ats"], board["slug"]))
            new[j["company"]] = j
    if new:
        with locked(COMPANIES) as companies:
            for company, j in new.items():
                c = companies.setdefault(company, {})
                c.setdefault("status", "candidate")
                c.setdefault("source", "job site role")
                c.setdefault("signal", f"{j['title']}, scored {j['score']}, found on its board")
                c.setdefault("added", str(date.today()))
        from radar.companies import sync
        sync()  # gives each its `board`
    return list(new)


def looked_up(careers: dict) -> dict:
    """{company: its board, or None} for companies looked up in the last GUESS_DAYS, so a second role from one of them
    doesn't send the same guesses to every vendor again. One whose careers address has changed since is looked up afresh."""
    since = str(date.today() - timedelta(days=GUESS_DAYS))
    return {name: tuple(rec["board"]) if rec.get("board") else None for name, rec in read(GUESSES).items()
            if rec.get("checked", "") > since and rec.get("careers") == careers.get(name)}


def remember(boards: dict, known: dict, careers: dict) -> None:
    """Keep what this run's lookups turned up, and forget the ones too old to trust."""
    today, since = str(date.today()), str(date.today() - timedelta(days=GUESS_DAYS))
    with locked(GUESSES) as kept:
        for name in [n for n, rec in kept.items() if rec.get("checked", "") <= since]:
            del kept[name]
        for name, hit in boards.items():
            if name and name not in known:
                kept[name] = {"checked": today, "board": list(hit) if hit else None, "careers": careers.get(name)}


def main(ids=()):
    """Every role waiting for a lookup, or with `ids` just those roles, now: their board, then a web search for any still
    without an employer's ad, whatever they scored and whether or not the automatic search is on."""
    store = read(JOBS)
    asked = [store[i] for i in ids if i in store]
    todo = asked if ids else waiting(store)
    fill = [] if ids else unfilled(store)
    found, filled, careers = {}, {}, careers_addresses()
    known = looked_up(careers)
    boards = dict(known)
    for j in todo:  # network outside the lock
        try:
            found[j["id"]] = find(j["company"], j["title"], boards, careers)
        except Exception as e:  # one bad board shouldn't stop the rest; this role is tried again next time
            print(f"{j['company']}: {type(e).__name__}")
            found[j["id"]] = False
    for j in fill:
        try:
            filled[j["id"]] = find(j["company"], j["title"], {same_company(j["company"]): (j["board"]["ats"], j["board"]["slug"], 0)})
        except Exception as e:
            print(f"{j['company']}: {type(e).__name__}")
    remember(boards, known, careers)
    now, again = datetime.now(timezone.utc).isoformat(timespec="seconds"), []
    with locked(JOBS) as store:
        for jid, hit in found.items():
            j = store.get(jid)
            if not j or hit is False:
                continue
            j["link_checked"] = str(date.today())
            if hit and save(j, hit, now):
                again.append(jid)
        for jid, hit in filled.items():
            if j := store.get(jid):
                j["location_checked"] = str(date.today())
                if hit and from_employer(j, hit.get("location"), hit.get("description")) and j.get("score") is not None:
                    again.append(jid)
    added = discover(read(JOBS))  # the board is confirmed to be theirs: from now on every role on it is read at source
    print(f"Checked {len(found)} roles, found {sum(bool(h) for h in found.values())} on company sites"
          + (f", now reading the board of {', '.join(added)}" if added else ""))
    cfg = load_config()
    if ids:
        still = read(JOBS)  # a role with an employer's link but no location from it is still worth a search
        again += run_ai(cfg, [j for j in asked if not found.get(j["id"]) and (j.get("link_kind") != "company" or location_unconfirmed(still.get(j["id"], j)))])
    elif cfg.get("links_ai_search"):
        again += run_ai(cfg)
    rescore(again)


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
