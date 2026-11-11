"""First run: from a pasted CV to about three good live roles, as fast as we can honestly make it.

The slow parts start before you ask for them. Boards are fetched as soon as the welcome page opens, with a hard time
limit, and the CV is read as soon as it's pasted. Find my roles then only has to rank what's already here, with no AI,
and ask one small AI question about the best few. Everything else waits until after you've seen your roles.

Whether first run shows comes from real state (a profile, saved roles, or a choice kept in settings.json), never from the
browser or the version, so a deploy can't bring it back. Nothing is saved until the roles are ready, and the profile goes
last, so a run that stops half way simply starts again.
"""
from __future__ import annotations

import csv
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

from radar import ai, boards, matching, paths, resolve, settings, triage, web
from radar.paths import DATA, JOBS, PROFILE, ROOT
from radar.store import locked, read

STARTER = ROOT / "starter-boards.csv"
EXAMPLE = ROOT / "profile" / "profile.example.md"
DRAFT = DATA / "first_run_profile.md"
TIMINGS = DATA / "first_run.json"
READ_PROMPT = ROOT / "prompts" / "first_read.md"
FIRST_LOOK = """

Also add "talk_to" to each role: the job title of the person this role most likely reports to, or who runs this team,
worked out from the ad, as a plain title with no notes. Only name a real person if the ad itself names them. Keep "why" under
25 words, speak to the candidate as "you", and make it about their own experience rather than the company."""

FETCH_SECONDS = 8    # a board that hasn't answered by now is left out, and the daily scan picks it up later
DETAIL_SECONDS = 3
FIND_BOARD_SECONDS = 4
READ_SECONDS = 40
ASK_SECONDS = 40
MIN_CV = 200
SHORTLIST = 6        # roles the AI looks at
BATCH = 3            # two small calls side by side, so reasons appear in two steps
TOP = 3
PER_COMPANY = 2
GOOD_ENOUGH = 5      # below this a role isn't worth showing as a top role, the same line as scoring's skip
PEOPLE_FOR = 3
REMOTE = ["remote (exactly)", "remote global", "remote worldwide", "remote anywhere", "anywhere"]
SUGGESTED = ["Stripe", "Spotify", "Figma", "Notion", "Airbnb", "Duolingo", "Monzo", "Databricks", "Doctolib", "Adyen", "Peloton", "Roblox"]

_lock = threading.RLock()


def _new() -> dict:
    return {"boards": 0, "answered": 0, "failed": 0, "roles": [], "fetching": False, "fetched": None, "ai": None, "ai_checking": False,
            "cv": "", "read": None, "reading": False, "read_error": "", "titles": None, "places": None, "favourites": [],
            "phase": "idle", "generation": 0, "matched": 0, "top": [], "message": "", "saved": False, "clock": {}}


RUN = _new()


def reset() -> None:
    with _lock:
        RUN.clear()
        RUN.update(_new())


# --- is this a new install? ---------------------------------------------------------------------------

def set_up() -> bool:
    """A real profile, saved roles or a finished first run. Any one of them means this install is already in use."""
    if settings.load().get("setup_finished"):
        return True
    try:
        if PROFILE.read_text().strip() not in ("", EXAMPLE.read_text().strip()):
            return True
    except OSError:
        pass
    try:
        return bool(read(JOBS))
    except (OSError, ValueError):
        return False


def needed() -> bool:
    return not set_up() and not settings.load().get("setup_skipped")


def allowed() -> bool:
    """First run can't touch an install in use. The one exception is changing your answers in the run that just finished."""
    return not set_up() or RUN["saved"]


def _guard() -> None:
    if not allowed():
        raise ValueError("Gus is already set up on this machine.")


def skip() -> dict:
    _guard()
    settings.remember("setup_skipped", _now())
    return {"ok": True}


def hide_get_started() -> dict:
    settings.remember("get_started_hidden", _now())
    return {"ok": True}


def get_started(cfg) -> list | None:
    """The things worth setting up after your first roles, each ticked off by what's really set. Only for installs that came through first run."""
    if not cfg.get("setup_finished") or cfg.get("get_started_hidden"):
        return None
    return [{"label": "Check for new roles every day", "done": bool(cfg.get("scheduler_enabled")), "link": "#settings/schedule"},
            {"label": "Get an alert on your phone", "done": bool(settings.secret("ntfy_topic")), "link": "#settings/notifications"},
            {"label": "Search more of the job market", "done": bool(cfg.get("source_adzuna") or cfg.get("source_jsearch")), "link": "#settings/sources"}]


def setup_state() -> dict:
    return {"needed": needed(), "can_run": allowed(), "get_started": get_started(settings.load())}


# --- fetching ------------------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mark(name: str) -> None:
    RUN["clock"].setdefault(name, time.monotonic())


def starter() -> list:
    with STARTER.open() as f:
        return list(csv.DictReader(f))


def listing(company: str, ats: str, slug: str) -> list:
    """Every open role on a board. Greenhouse lists titles only, so its details are fetched for the shortlist alone."""
    if ats == "greenhouse":
        d = web.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        rows = [{"id": f"gh:{slug}:{j['id']}", "company": company, "title": j["title"], "url": j["absolute_url"],
                 "posted": j.get("first_published") or j.get("updated_at"), "description": "",
                 "location": (j.get("location") or {}).get("name", "")} for j in d.get("jobs", [])]
    else:
        rows = boards.SOURCES[ats](company, slug)
    for r in rows:
        r.update(description=(r.get("description") or "")[:2500], _ats=ats, _slug=slug)
    return rows


def _board(row: dict, deadline: float) -> None:
    web.time_limit(deadline)
    try:
        roles = listing(row["company"], row["ats"], row["slug"])
        with _lock:
            RUN["roles"] += roles
            RUN["answered"] += 1
    except Exception:  # a dead or slow board never holds up the rest
        with _lock:
            RUN["failed"] += 1
    finally:
        web.time_limit(None)


def _fetch() -> None:
    boards = starter()
    RUN["boards"] = len(boards)
    deadline = time.monotonic() + FETCH_SECONDS
    pool = ThreadPoolExecutor(max_workers=max(1, min(40, len(boards))))
    wait([pool.submit(_board, b, deadline) for b in boards], timeout=FETCH_SECONDS + 1)
    pool.shutdown(wait=False, cancel_futures=True)
    with _lock:
        RUN.update(fetching=False, fetched=_now())
        _mark("fetched")


def _check_ai() -> None:
    result = ai.status()
    with _lock:
        RUN.update(ai=result, ai_checking=False)


def warm(recheck=False) -> None:
    """Start the slow parts now, so they're done before you ask."""
    with _lock:
        _mark("opened")
        if not RUN["fetching"] and not RUN["fetched"]:
            RUN["fetching"] = True
            threading.Thread(target=_fetch, daemon=True).start()
        if not RUN["ai_checking"] and (recheck or RUN["ai"] is None):
            RUN["ai_checking"] = True
            threading.Thread(target=_check_ai, daemon=True).start()


def _find_board(name: str) -> None:
    """A company you typed: try the likely addresses at once, and take the board with the most roles."""
    deadline = time.monotonic() + FIND_BOARD_SECONDS

    def probe(t):
        web.time_limit(deadline)
        try:
            return t, resolve.probe(*t)
        except Exception:
            return t, None
        finally:
            web.time_limit(None)

    tries = [(a, s) for s in resolve.slugs(name)[:3] for a in ("greenhouse", "ashby", "lever", "workable")]
    pool = ThreadPoolExecutor(max_workers=len(tries))
    done, _ = wait([pool.submit(probe, t) for t in tries], timeout=FIND_BOARD_SECONDS + .5)
    pool.shutdown(wait=False, cancel_futures=True)
    hits = [f.result() for f in done if f.result()[1]]
    best = max(hits, key=lambda h: h[1])[0] if hits else None
    if best:
        _board({"company": name, "ats": best[0], "slug": best[1]}, time.monotonic() + FETCH_SECONDS)
    with _lock:
        for f in RUN["favourites"]:
            if f["name"] == name:
                f.update(finding=False, ats=best[0] if best else None, slug=best[1] if best else None)


def _set_favourites(names: list) -> None:
    known = {r["company"].lower(): r for r in starter()}
    keep, had = [], {f["name"].lower(): f for f in RUN["favourites"]}
    for name in dict.fromkeys(str(n).strip()[:60] for n in names):
        if not name:
            continue
        if name.lower() in had:
            keep.append(had[name.lower()])
        elif name.lower() in known:
            r = known[name.lower()]
            keep.append({"name": r["company"], "ats": r["ats"], "slug": r["slug"], "finding": False})
        else:
            keep.append({"name": name, "ats": None, "slug": None, "finding": True})
            threading.Thread(target=_find_board, args=(name,), daemon=True).start()
    RUN["favourites"] = keep


# --- reading the CV ------------------------------------------------------------------------------------

def parse_read(text: str) -> dict:
    """Only what we expect, in the shape we expect. A bad reply must never reach settings or the profile."""
    d = json.loads(text[text.index("{"): text.rindex("}") + 1])

    def strings(key, most):
        v = d.get(key)
        return [x.strip() for x in v if isinstance(x, str) and x.strip()][:most] if isinstance(v, list) else []

    def line(key, most):
        v = d.get(key)
        return " ".join(v.split())[:most] if isinstance(v, str) else ""

    code = line("country_code", 20).lower()
    titles = [re.sub(r"^((senior|sr\.?|principal|staff|junior|jr\.?|lead)\s+)+", "", t.lower()) for t in strings("titles", 6)]
    return {"titles": list(dict.fromkeys(titles)), "places": strings("places", 8), "city": line("city", 60),
            "country_code": code if re.fullmatch(r"[a-z]{2}", code) else "", "currency": line("currency", 3), "level": line("level", 8).lower(),
            "keywords": [k.lower() for k in strings("keywords", 12)], "employers": strings("employers", 12), "edge": strings("edge", 3)}


def _read(cv: str) -> None:
    try:
        read, problem = parse_read(ai.run(READ_PROMPT.read_text().replace("{{CV}}", cv[:12000]), task="quick", timeout=READ_SECONDS)), ""
    except Exception as e:
        read, problem = None, str(e)[:200]
    with _lock:
        if RUN["cv"] != cv:
            return  # you've pasted something newer
        RUN.update(read=read, reading=False, read_error=problem)
        if read:
            RUN["titles"] = RUN["titles"] or read["titles"]
            RUN["places"] = RUN["places"] or read["places"]
        _mark("read")


# --- the shortlist: no AI, so it's immediate -------------------------------------------------------------

def _clean(values) -> list:
    return [" ".join(str(v).split())[:80] for v in values or [] if str(v).strip()]


def location_lines(places: list) -> list:
    out = []
    for p in places:
        out += REMOTE if p.lower() == "remote" else [p]
    return list(dict.fromkeys(out))


def matcher(titles: list, places: list) -> dict:
    """The same test the daily scan uses, so first run shows nothing the scan wouldn't have found."""
    raw = settings.load()
    pattern = lambda v: re.compile(settings.to_pattern(v), re.I)  # noqa: E731
    return {"title_include": pattern("\n".join(titles)), "title_exclude": pattern(raw.get("title_exclude")),
            "title_exclude_except": pattern(raw.get("title_exclude_except")), "location_include": pattern("\n".join(location_lines(places)))}


def level_of(title: str) -> int:
    t = title.lower()
    if re.search(r"\b(head|director|vp|vice president|chief)\b", t):
        return 3
    if re.search(r"\b(senior|sr|staff|principal|lead)\b", t):
        return 2
    return 0 if re.search(r"\b(junior|jr|associate|graduate|entry)\b", t) else 1


def rank(job: dict, titles: list, places: list, read: dict, loved: set) -> float:
    title, where = job["title"].lower(), job["location"].lower()
    score = 0.0
    for i, t in enumerate(titles):
        if t.lower() in title:
            score += 6 - min(i, 4) * .5
            break
    for i, p in enumerate(p for p in places if p.lower() != "remote"):
        if p.lower() in where:
            score += 3 if i == 0 else 1.5
            break
    else:
        score += 1  # remote: good, but not as good as somewhere you can get to
    wanted = {"junior": 0, "mid": 1, "senior": 2, "exec": 3}.get(read.get("level"))
    if wanted is not None:
        score -= abs(level_of(title) - wanted) * 1.5
    score += min(5, len(fits(job, read))) * .6
    return score + (3 if job["company"].lower() in loved else 0)


def fits(job: dict, read: dict) -> list:
    text = f"{job['title']} {job.get('description') or ''}".lower()
    return [k for k in read.get("keywords", []) if k in text]


def spread(rows: list, n: int) -> list:
    """Best first, with no one company taking over."""
    seen, out = {}, []
    for r in rows:
        seen[r["company"]] = seen.get(r["company"], 0) + 1
        if seen[r["company"]] <= PER_COMPANY:
            out.append(r)
        if len(out) == n:
            break
    return out


def card(job: dict, read: dict, places: list) -> dict:
    """What shows the moment a role makes the list. The AI's reason replaces this one when it arrives."""
    found = fits(job, read)[:3]
    place = next((p for p in places if p.lower() in job["location"].lower()), job["location"][:40])
    why = f"Mentions {', '.join(found)}, from your CV." if found else "Fits the roles you're looking for."
    return {"id": job["id"], "company": job["company"], "title": job["title"], "location": job["location"], "url": job["url"],
            "place": place, "why": why, "score": None, "flags": [], "talk_to": "", "reason_ready": False}


def _details(rows: list) -> None:
    """Full ads for the shortlisted Greenhouse roles. Anything too slow is scored on its title alone."""
    deadline = time.monotonic() + DETAIL_SECONDS

    def one(r):
        web.time_limit(deadline)
        try:
            d = web.get_json(f"https://boards-api.greenhouse.io/v1/boards/{r['_slug']}/jobs/{r['id'].rsplit(':', 1)[-1]}")
            r["description"] = web.strip_html(d.get("content", ""))[:2500]
            r["location"] = " / ".join(dict.fromkeys(filter(None, [r["location"]] + [o.get("name", "") for o in d.get("offices") or []])))
        except Exception:
            pass
        finally:
            web.time_limit(None)

    todo = [r for r in rows if r.get("_ats") == "greenhouse" and not r["description"]]
    if todo:
        pool = ThreadPoolExecutor(max_workers=len(todo))
        wait([pool.submit(one, r) for r in todo], timeout=DETAIL_SECONDS + .5)
        pool.shutdown(wait=False, cancel_futures=True)


def profile_text(cv: str, titles: list, places: list, read: dict) -> str:
    edge = "\n".join(f"- {e}" for e in read.get("edge", [])) or "- Add the two or three things that set you apart"
    return (f"# Candidate profile\n\n## What I want\n- Target roles: {', '.join(titles)}\n- Locations: {', '.join(places)}\n"
            f"- Level: {read.get('level') or 'not stated'}\n- Domains: {', '.join(read.get('keywords', [])[:6]) or 'open'}\n\n"
            f"## Strongest edge\n{edge}\n\n## CV\n{cv.strip()}\n")


# --- find my roles ---------------------------------------------------------------------------------------

def _current(gen: int) -> bool:
    return RUN["generation"] == gen


def _wait(ready, seconds: float, gen: int) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and _current(gen) and not ready():
        time.sleep(.05)


def _stop(gen: int, phase: str, message: str) -> None:
    with _lock:
        if _current(gen):
            RUN.update(phase=phase, message=message)


def _find(gen: int) -> None:
    _wait(lambda: not RUN["fetching"], FETCH_SECONDS + 2, gen)
    _wait(lambda: not RUN["reading"], READ_SECONDS, gen)
    _wait(lambda: not any(f["finding"] for f in RUN["favourites"]), FIND_BOARD_SECONDS + 1, gen)
    with _lock:
        titles, places, read = list(RUN["titles"] or []), list(RUN["places"] or []), RUN["read"] or {}
        pool = list(RUN["roles"])
    if not _current(gen):
        return
    if not titles or not places:
        return _stop(gen, "form", "Gus couldn't read that CV. Add the roles and places you want, then try again.")
    loved = {f["name"].lower() for f in RUN["favourites"]}
    cfg = matcher(titles, places)
    seen, matched = set(), []
    for job in pool:
        same = (job["company"].lower(), job["title"].lower())  # one role, posted once per city, is still one role
        if same not in seen and matching.matches(job, cfg):
            seen.add(same)
            matched.append(job)
    by_rank = lambda j: rank(j, titles, places, read, loved)  # noqa: E731
    matched.sort(key=by_rank, reverse=True)
    with _lock:
        if not _current(gen):
            return
        RUN.update(matched=len(matched), top=[card(j, read, places) for j in spread(matched, TOP)], phase="reasons" if matched else "none")
        _mark("shortlist")
    if not matched:
        return _save(gen, [], titles, places, read)

    near = spread(matched, SHORTLIST * 2)
    _details(near)
    best = spread(sorted(near, key=by_rank, reverse=True), SHORTLIST)
    with _lock:
        if not _current(gen):
            return
        RUN["top"] = [card(j, read, places) for j in best]
    DRAFT.parent.mkdir(exist_ok=True)
    DRAFT.write_text(profile_text(RUN["cv"][:4000], titles, places, read))  # enough to judge a role, and quicker to read

    def ask(chunk):
        try:
            for r in triage.ask(DRAFT, chunk, task="quick", chars=1200, also=FIRST_LOOK, timeout=ASK_SECONDS):
                _reason(gen, r)
        except Exception as e:
            print(f"First look: {e}")

    with ThreadPoolExecutor(max_workers=2) as batches:
        list(batches.map(ask, [best[i:i + BATCH] for i in range(0, len(best), BATCH)]))
    with _lock:
        if _current(gen):
            RUN["top"].sort(key=lambda c: c["score"] or 0, reverse=True)
            if any(c["reason_ready"] for c in RUN["top"]) and not any((c["score"] or 0) >= GOOD_ENOUGH for c in RUN["top"]):
                RUN["phase"] = "none"  # a list of poor fits is worse than saying so
            _mark("reasons")
    _save(gen, best, titles, places, read)


def _reason(gen: int, r: dict) -> None:
    with _lock:
        if not _current(gen):
            return
        for c in RUN["top"]:
            if c["id"] == r["id"]:
                flags = r.get("flags") if isinstance(r.get("flags"), list) else []
                talk = r.get("talk_to") if isinstance(r.get("talk_to"), str) else ""
                c.update(score=r["score"], why=r["why"].strip(), flags=flags[:3], talk_to=talk.strip()[:80], reason_ready=True)
        RUN["top"].sort(key=lambda c: (c["score"] or 0), reverse=True)
        _mark("first_reason")


def changes_for(titles: list, places: list, read: dict) -> dict:
    """Your answers as settings, so the daily scan carries on from here."""
    city, others = read.get("city", ""), [p for p in places if p.lower() not in ("remote", read.get("city", "").lower())]
    rows = ([f"{city}: {city.lower()}"] if city else []) + ["Remote: remote, anywhere"]
    if others:
        rows.append(f"{others[0]}: {', '.join(o.lower() for o in others)}")
    title = titles[0] if titles else ""
    changes = {"title_include": "\n".join(titles), "location_include": "\n".join(location_lines(places)), "sources_searches": "\n".join(titles),
               "sources_location": city, "sources_country": read.get("country_code", ""), "salary_symbol": read.get("currency", ""),
               "my_employers": "\n".join(read.get("employers", [])), "where_groups": "\n".join(rows + ["Elsewhere: *"]),
               "social_queries": f'"hiring" "{title}" {city}'.strip() + f'\n"my team is hiring" {title}' if title else ""}
    return {k: v for k, v in changes.items() if v or k == "sources_location"}


def _keep_boards(rows: list) -> None:
    """Your companies, plus the starter boards that have something for you. Anything you already watch stays."""
    path, example = paths.WATCHLIST, ROOT / "watchlist.example.csv"
    untouched = path.exists() and path.read_text() == example.read_text()  # copied in by a read, not chosen by anyone
    have = list(csv.DictReader(path.open())) if path.exists() and not untouched else []
    known = {r["slug"] for r in have}
    with path.open("a" if have else "w", newline="") as f:
        out = csv.writer(f)
        if not have:
            out.writerow(["company", "ats", "slug", "sector"])
        for company, ats, slug, sector in rows:
            if slug not in known:
                known.add(slug)
                out.writerow([company, ats, slug, sector])


def save_favourites(favourites: list) -> None:
    """Companies you love: where neighbour discovery starts (prompts/watchlist.md). Scoring never sees them."""
    from radar.companies import COMPANIES
    with locked(COMPANIES) as companies:
        for f in favourites:
            c = companies.setdefault(f["name"], {})
            c.update(favourite=True, added=c.get("added") or str(datetime.now(timezone.utc).date()))
            c.setdefault("status", "watched")
            if f.get("ats"):
                c["board"] = f"{f['ats']}:{f['slug']}"


def _save(gen: int, best: list, titles: list, places: list, read: dict) -> None:
    with _lock:
        if not _current(gen):
            return
        by_id, cards = {j["id"]: j for j in best}, list(RUN["top"])
        favourites, cv = list(RUN["favourites"]), RUN["cv"]
        found = {j["company"]: (j["_ats"], j["_slug"]) for j in RUN["roles"]
                 if matching.matches(j, matcher(titles, places))} if best else {}
    settings.save(changes_for(titles, places, read))
    sectors = {r["company"]: r["sector"] for r in starter()}
    _keep_boards([(f["name"], f["ats"], f["slug"], "favourite") for f in favourites if f.get("ats")] +
                 [(c, a, s, sectors.get(c, "starter")) for c, (a, s) in found.items()])
    save_favourites(favourites)
    now = _now()
    with locked(JOBS) as store:
        for jid in [k for k, j in store.items() if j.get("first_run") and j.get("stage") == "new"]:
            del store[jid]  # you changed your answers: these came from the old ones
        for c in cards:
            job = by_id.get(c["id"])
            if job and c["reason_ready"]:
                store.setdefault(c["id"], {k: job.get(k) for k in ("id", "company", "title", "location", "url", "posted", "description")}).update(
                    first_seen=now, last_seen=now, stage="new" if c["score"] >= GOOD_ENOUGH else "skipped", score=c["score"], why=c["why"], flags=c["flags"],
                    talk_to=c["talk_to"], source="board", first_run=True)
    PROFILE.parent.mkdir(exist_ok=True)
    PROFILE.write_text(profile_text(cv, titles, places, read))  # last: from here on this install counts as set up
    settings.remember("setup_finished", now)
    DRAFT.unlink(missing_ok=True)
    with _lock:
        RUN.update(saved=True, phase="done" if RUN["phase"] != "none" else "none")
        _mark("saved")
        TIMINGS.write_text(json.dumps({"at": now, "seconds": timings(), "boards": RUN["boards"], "answered": RUN["answered"],
                                       "roles": len(RUN["roles"]), "matched": RUN["matched"]}, indent=2))
    threading.Thread(target=_after, args=(gen, [c["company"] for c in cards[:PEOPLE_FOR] if c["reason_ready"]]), daemon=True).start()


def _after(gen: int, companies: list) -> None:
    """After the wow: the rest of the roles in the background, and real names for the top companies."""
    from radar import people, service
    service.start("scan")

    def find(name):
        try:
            people.map_account(name)
        except Exception as e:  # finding a name is a bonus, never a failure
            print(f"People at {name}: {e}")

    with ThreadPoolExecutor(max_workers=max(1, len(companies))) as pool:
        list(pool.map(find, dict.fromkeys(companies)))
    with _lock:
        if _current(gen):
            RUN["people_done"] = True


# --- what the page calls ----------------------------------------------------------------------------------

def update(body: dict) -> dict:
    """Everything the welcome page sends. Safe to call on every change: each part only starts what isn't already running."""
    _guard()
    warm(bool(body.get("recheck")))
    with _lock:
        cv = str(body.get("cv") or "")[:40000].strip()
        if cv and cv != RUN["cv"]:
            RUN.update(cv=cv, read=None, read_error="", titles=None, places=None)
            if len(cv) >= MIN_CV:
                RUN["reading"] = True
                _mark("pasted")
                threading.Thread(target=_read, args=(cv,), daemon=True).start()
            else:
                RUN["reading"] = False
        if "favourites" in body:
            _set_favourites(body["favourites"] or [])
        if body.get("titles") is not None:
            RUN["titles"] = _clean(body["titles"])
        if body.get("places") is not None:
            RUN["places"] = _clean(body["places"])
        if body.get("go"):
            if len(RUN["cv"]) < MIN_CV:
                raise ValueError("Paste your CV first.")
            RUN["generation"] += 1
            RUN.update(phase="finding", top=[], matched=0, message="", people_done=False)
            for k in ("go", "shortlist", "first_reason", "reasons", "saved"):
                RUN["clock"].pop(k, None)
            _mark("go")
            threading.Thread(target=_find, args=(RUN["generation"],), daemon=True).start()
        elif body.get("change"):
            RUN["generation"] += 1
            RUN.update(phase="form", message="")
    return status()


def connect(key: str) -> dict:
    _guard()
    if not key.strip():
        raise ValueError("Paste your API key first.")
    settings.set_secret("anthropic_api_key", key)
    settings.save({"ai_provider": "anthropic_api"})
    warm(True)
    return status()


def timings() -> dict:
    """Seconds from each starting point. `go` is the button; `pasted` is when the CV landed."""
    clock = RUN["clock"]
    out = {}
    for start in ("opened", "pasted", "go"):
        if start in clock:
            out[f"from_{start}"] = {k: round(v - clock[start], 1) for k, v in clock.items() if v >= clock[start] and k != start}
    return out


def status() -> dict:
    from radar.paths import PEOPLE
    with _lock:
        out = {k: RUN[k] for k in ("boards", "answered", "failed", "fetching", "ai", "reading", "read_error", "titles", "places",
                                   "favourites", "phase", "matched", "message", "saved")}
        out["roles"] = len(RUN["roles"])
        out["people_done"] = RUN.get("people_done", False)
        out["top"] = [] if RUN["phase"] == "none" else [dict(c) for c in RUN["top"] if not c["reason_ready"] or c["score"] >= GOOD_ENOUGH][:TOP]
        out["checked"] = len(RUN["top"])
        out["timings"] = timings()
    boards = [r["company"] for r in starter()]
    out["suggested"] = [n for n in SUGGESTED if n in boards] or boards[:12]
    if out["saved"]:
        people = read(PEOPLE, {}).values()
        for c in out["top"]:
            c["people"] = [{"name": p.get("name"), "role": p.get("role"), "url": p.get("url")} for p in people if p.get("company") == c["company"]][:2]
    return out
