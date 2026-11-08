"""Job search sources beyond company boards: Adzuna and JSearch.

    python3 -m radar.sources                         # run whatever is due (the service does this once a day)
    python3 -m radar.sources status                  # used, left and reset dates
    python3 -m radar.sources record adzuna 42 [date] # count requests made outside Gus

It runs once a day on its own and as a step of a full run, never from Check roles. Each source decides for itself: it
runs only when it's due and has allowance left, so running this more often can't use more requests.
Requests are counted before they're sent, so one that fails still counts. Counts live in data/sources_state.json,
and your limits in Settings: neither is in the repo.
"""
from __future__ import annotations

import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from radar import jobs as J
from radar.jobs import same_company, same_title  # one definition, shared with scan and links
from radar.scan import DATA, JOBS, UA, left_out, load_config, matches, strip_html
from radar.settings import secret
from radar.store import locked

STATE = DATA / "sources_state.json"
LABEL = {"adzuna": "Adzuna", "jsearch": "JSearch"}
KEYS = {"adzuna": ("adzuna_app_id", "adzuna_app_key"), "jsearch": ("jsearch_api_key",)}
ADZUNA = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
JSEARCH = "https://api.openwebninja.com/jsearch/search"
JSEARCH_USAGE = "https://api.openwebninja.com/usage"
REFUSED = (401, 403, 429)  # a refusal won't improve by trying again
TIME_LIMIT = 900           # seconds per source, so a slow service can't use up the whole run
FULL_PAGE = {"adzuna": 50, "jsearch": 1}
PERIODS = ("day", "week", "month")
OVERLAP = 2                # days re-checked each run, so nothing posted around a run is missed


class Failed(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}" if code else "no answer")
        self.code = code


def call(url, headers=None):
    """Errors carry only a status code: Adzuna's key is in the address, and it must never reach a log."""
    req = urllib.request.Request(url, headers={**UA, "Accept-Encoding": "identity", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise Failed(e.code) from None
    except (OSError, ValueError, http.client.HTTPException):
        raise Failed(0) from None


# --- counting and limits ------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(st) -> None:
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2))


def spend(st, name, n=1, on=None) -> None:
    days = st.setdefault(name, {}).setdefault("days", {})
    day = str(on or date.today())
    days[day] = days.get(day, 0) + n
    keep = str(date.today() - timedelta(days=45))
    for d in [d for d in days if d < keep]:
        del days[d]
    save_state(st)


def resets(period, today) -> date:
    if period == "day":
        return today + timedelta(days=1)
    if period == "week":
        return today + timedelta(days=7 - today.weekday())
    return (today.replace(day=1) + timedelta(days=32)).replace(day=1)


def used_in(period, days, today) -> int:
    start = {"day": today, "week": today - timedelta(days=today.weekday()), "month": today.replace(day=1)}[period]
    return sum(n for d, n in days.items() if d >= str(start))


def limits(name, cfg, st, today=None) -> list:
    """One row per limit. Adzuna has no usage counter, so its figures are Gus's own count and its resets are estimates."""
    today = today or date.today()
    s = st.get(name, {})
    if name == "adzuna":
        rows = []
        for p in PERIODS:
            limit = int(cfg.get(f"adzuna_limit_{p}", 0))
            limit = min(limit, allowed(name, cfg)) if p == "month" else limit
            used = used_in(p, s.get("days", {}), today)
            rows.append({"period": p, "limit": limit, "used": used, "left": max(0, limit - used),
                         "resets": str(resets(p, today)), "exact": False})
        return rows
    q = s.get("quota")
    if not q:
        return []
    limit = min(q["limit"], allowed(name, cfg))
    return [{"period": "month", "limit": limit, "used": q["used"], "left": max(0, min(q["remaining"], limit - q["used"])),
             "resets": q["reset_at"][:10], "exact": True}]


def per_run(name, cfg) -> int:
    """The monthly allowance shared evenly across runs, so a free plan lasts the whole month."""
    every = int(cfg.get(f"{name}_every_days", 7))
    return max(1, int(allowed(name, cfg) * every / 30))


def allowed(name, cfg) -> int:
    return int(cfg.get(f"{name}_allow_month", 0))


def budget(name, cfg, st) -> int:
    """How many requests this run may make. No confirmed figure means none, so a fault can't lead to overspending."""
    if name == "jsearch" and (st.get(name, {}).get("quota") or {}).get("checked") != str(date.today()):
        return 0
    return max(0, min([per_run(name, cfg)] + [r["left"] for r in limits(name, cfg, st)]))


def is_due(name, cfg, st, today=None) -> bool:
    last = st.get(name, {}).get("last_run")
    return not last or ((today or date.today()) - date.fromisoformat(last)).days >= int(cfg.get(f"{name}_every_days", 7))


def refresh_quota(st) -> bool:
    """Ask JSearch how much is left. This doesn't use up a request."""
    url = JSEARCH_USAGE + "?" + urllib.parse.urlencode({"api_id": "jsearch"})
    try:
        data = call(url, {"x-api-key": secret("jsearch_api_key")})["data"]
        q = data["quotas"][0]
    except (Failed, KeyError, IndexError, TypeError):
        return False
    if not (all(isinstance(q.get(k), int) for k in ("limit", "used", "remaining")) and isinstance(q.get("reset_at"), str)):
        return False  # a reply we can't trust counts as no reply
    st.setdefault("jsearch", {})["quota"] = {"limit": q["limit"], "used": q["used"], "remaining": q["remaining"], "reset_at": q["reset_at"],
                                             "free": bool(((data.get("plan") or {}).get("is_free"))), "checked": str(date.today())}
    save_state(st)
    return True


def searches(cfg) -> list:
    return [t.strip() for t in str(cfg.get("sources_searches", "")).splitlines() if t.strip()]


def plan(name, cfg) -> dict:
    """The most a source could use, worked out from the settings. Real use is usually lower: searches stop when they run dry."""
    every, pages = int(cfg.get(f"{name}_every_days", 7)), int(cfg.get(f"{name}_pages_per_search", 3))
    wanted = len(searches(cfg)) * pages
    month = round(wanted * 30 / every)
    return {"wanted": wanted, "per_run": per_run(name, cfg), "month": month, "allowed": allowed(name, cfg),
            "line": f"{len(searches(cfg))} searches × {pages} pages, every {every} day{'' if every == 1 else 's'}: up to {month} of the {allowed(name, cfg)} you've allowed a month"}


def warnings(name, cfg, st) -> list:
    """Choices that won't do what you'd expect, shown before any requests are spent."""
    s, p, out = st.get(name, {}), plan(name, cfg), []
    label, every, quota = LABEL[name], int(cfg.get(f"{name}_every_days", 7)), st.get(name, {}).get("quota") or {}
    if cfg.get(f"source_{name}") and not all(secret(k) for k in KEYS[name]):
        out.append(f"{label} is on but won't run: its key is missing.")
    if not searches(cfg):
        out.append("No searches set, so no roles will be found.")
    if not re.fullmatch(r"[a-z]{2}", str(cfg.get("sources_country", ""))):
        out.append("Country code should be two lower-case letters, like gb.")
    if p["wanted"] > p["per_run"]:
        better = next((d for d in (2, 3, 7, 14, 30) if d > every and allowed(name, cfg) * d / 30 >= p["wanted"]), None)
        hint = f" Running every {better} days would cover them." if better else " Use fewer searches or pages."
        free = "On a free plan, each" if quota.get("free") else "Each"
        out.append(f"{free} run only gets {p['per_run']} requests but could need {p['wanted']}, so later searches may not run.{hint}")
    if name == "adzuna" and allowed(name, cfg) > int(cfg.get("adzuna_limit_month", 0)):
        out.append("You've allowed more each month than your Adzuna monthly limit, so the limit applies.")
    if name == "jsearch" and quota and allowed(name, cfg) > quota["limit"]:
        out.append(f"You've allowed {allowed(name, cfg)} a month, but your JSearch plan gives {quota['limit']}.")
    rows = limits(name, cfg, st)
    low = next((r for r in rows if 0 < r["left"] < p["per_run"]), None)
    full = next((r for r in rows if r["left"] <= 0), None)
    if full:
        out.append(f"{label} is at its {full['period']}ly limit and paused until {nice(full['resets'])}.")
    elif low:
        out.append(f"Only {low['left']} {label} requests left until {nice(low['resets'])}, so the next run will stop early.")
    if s.get("cut"):
        out.append(f"{s['cut']} search{'es' if s['cut'] > 1 else ''} had more roles than the pages allowed, so older ones were missed. Raise the pages, or narrow the search.")
    return out


def nice(day) -> str:
    return date.fromisoformat(str(day)).strftime("%a %-d %b")


def status() -> list:
    """What the Health page shows. Reads saved figures only, so opening a page can't use a request."""
    cfg, st = load_config(), load_state()
    out = []
    for name in LABEL:
        s = st.get(name, {})
        rows = limits(name, cfg, st)
        last = s.get("last_run")
        next_run = str(date.fromisoformat(last) + timedelta(days=int(cfg.get(f"{name}_every_days", 7)))) if last else None
        out.append({"name": name, "label": LABEL[name], "on": bool(cfg.get(f"source_{name}")),
                    "has_key": all(secret(k) for k in KEYS[name]), "limits": rows, "at_limit": any(r["left"] <= 0 for r in rows),
                    "last_run": last, "next_run": next_run, "last_result": s.get("last_result", ""),
                    "plan": plan(name, cfg)["line"], "warnings": warnings(name, cfg, st)})
    return out


# --- fetching: each returns normalised roles -----------------------------------

def adzuna(term, page, cfg, age):
    q = {"app_id": secret("adzuna_app_id"), "app_key": secret("adzuna_app_key"), "title_only": term, "max_days_old": age,
         "results_per_page": 50, "sort_by": "date", "content-type": "application/json"}
    if cfg.get("sources_location"):
        q.update(where=cfg["sources_location"], distance=cfg.get("sources_distance_km", 30))
    d = call(ADZUNA.format(country=cfg.get("sources_country", "gb"), page=page) + "?" + urllib.parse.urlencode(q))
    out = []
    for r in d.get("results", []):
        loc = r.get("location") or {}
        country = (loc.get("area") or [""])[0]
        out.append({"id": f"adzuna:{r['id']}", "company": ((r.get("company") or {}).get("display_name") or "").strip(),
                    "title": (r.get("title") or "").strip(), "url": re.sub(r"[?&]utm_source=[^&]*", "", r.get("redirect_url") or ""), "via": "Adzuna", "posted": r.get("created"),
                    "location": ", ".join(filter(None, [loc.get("display_name"), country])),
                    "salary_max": None if str(r.get("salary_is_predicted")) != "0" else r.get("salary_max"),  # Adzuna's guesses don't count
                    "description": strip_html(r.get("description") or "")})
    return out


def jsearch(term, page, cfg, age):
    place = cfg.get("sources_location") or {"gb": "United Kingdom"}.get(cfg.get("sources_country", "gb"), "")
    since = age - OVERLAP  # JSearch only has fixed windows, so the overlap would otherwise push a weekly run up to a month
    window = "today" if since <= 1 else "3days" if since <= 3 else "week" if since <= 7 else "month"
    q = {"query": f"{term} in {place}".strip(), "country": cfg.get("sources_country", "gb"), "date_posted": window, "page": page, "num_pages": 1}
    d = call(JSEARCH + "?" + urllib.parse.urlencode(q), {"x-api-key": secret("jsearch_api_key")})
    data = d.get("data")
    country = "UK" if cfg.get("sources_country", "gb") == "gb" else cfg.get("sources_country", "").upper()
    out = []
    for r in (data if isinstance(data, list) else (data or {}).get("jobs", [])):
        place_name = re.sub(r"\s+", " ", r.get("job_location") or "").strip()
        place_name = "Remote" if r.get("job_is_remote") or place_name == "Anywhere" else place_name
        out.append({"id": f"jsearch:{r['job_id']}", "company": (r.get("employer_name") or "").strip(), "title": (r.get("job_title") or "").strip(),
                    "url": r.get("job_apply_link"), "via": r.get("job_publisher"), "posted": r.get("job_posted_at_datetime_utc"),
                    "location": ", ".join(filter(None, [place_name, country])), "description": r.get("job_description") or ""})
    return out


FETCH = {"adzuna": adzuna, "jsearch": jsearch}


# --- the same clean-up for every source -----------------------------------------

def ad_text(j) -> str:
    """The same advert under a slightly different title. Short extracts are too alike to compare."""
    text = re.sub(r"\W+", " ", (j.get("description") or "").lower())[:400]
    return f"{same_company(j.get('company'))}|{text}" if len(text) >= 200 else ""


def hits(pattern, text) -> bool:
    return bool(pattern and pattern.pattern and pattern.search(text))  # an empty setting must not match everything


def clean(rows, store, cfg, today=None, now=None):
    """Keep real, fresh, wanted roles that Gus doesn't already have. Returns the roles and why others went."""
    cutoff = str((today or date.today()) - timedelta(days=int(cfg.get("sources_max_age_days", 14))))
    seen = {(same_company(j.get("company")), same_title(j.get("title"))) for j in store.values()}
    texts = {ad_text(j) for j in store.values()} - {""}
    alias = J.aliases(store)
    kept, dropped = [], dict.fromkeys(("old", "no employer", "agency", "repeat", "not a match"), 0)
    for j in rows:
        key = (same_company(j["company"]), same_title(j["title"]))
        if j["id"] in store:
            continue
        if (have := J.find_match(store, j, alias)) is not None:
            J.absorb(store[have], j, now or datetime.now(timezone.utc).isoformat(timespec="seconds"))
            dropped["repeat"] += 1
            continue
        if not j.get("posted") or str(j["posted"])[:10] < cutoff:
            why = "old"
        elif not key[0]:
            why = "no employer"
        elif left_out(j["company"], cfg, "source_company_exclude") or hits(cfg.get("source_text_exclude"), j["description"][:6000]):
            why = "agency"
        elif key in seen or ad_text(j) in texts:
            why = "repeat"
        elif not matches(j, cfg):
            why = "not a match"
        else:
            seen.add(key)
            texts.add(ad_text(j))
            kept.append(j)
            continue
        dropped[why] += 1
    return kept, dropped


# --- a run -----------------------------------------------------------------------

def collect(name, cfg, st, limit, age):
    """Spend at most `limit` requests. A refusal stops the source; a passing fault only skips that search."""
    run = {"rows": {}, "spent": 0, "empty": 0, "faults": 0, "cut": 0, "stopped": ""}
    started = time.monotonic()
    pages = int(cfg.get(f"{name}_pages_per_search", 3))
    for term in searches(cfg):
        for page in range(1, pages + 1):
            if run["spent"] >= limit:
                run["stopped"] = f"stopped at the {limit} request limit"
                return run
            if time.monotonic() - started > TIME_LIMIT:
                run["stopped"] = "stopped: taking too long"
                return run
            spend(st, name)  # before the call: a request that times out may still have counted
            run["spent"] += 1
            try:
                found = FETCH[name](term, page, cfg, age)
            except Failed as e:
                if e.code in REFUSED:
                    run["stopped"] = f"refused ({e})"
                    return run
                run["faults"] += 1
                break
            except (KeyError, TypeError, AttributeError):  # a reply in an unexpected shape
                run["faults"] += 1
                break
            fresh = [j for j in found if j["id"] not in run["rows"]]
            run["rows"].update((j["id"], j) for j in fresh)
            run["empty"] += page == 1 and not found
            if len(found) < FULL_PAGE[name] or len(fresh) < 3:
                break
            run["cut"] += page == pages and FULL_PAGE[name] > 1  # JSearch has no page size to judge this by
    return run


def tell_limit(name, st, row) -> None:
    """One notification per limit reached, not one per run."""
    s = st.setdefault(name, {})
    mark = f"{row['period']}:{row['resets']}"
    if s.get("limit_notified") != mark:
        from radar.notify import notify
        s["limit_notified"] = mark
        save_state(st)
        notify(f"Gus: {LABEL[name]} limit reached", "", f"Paused until {row['resets']}, when the {row['period']}ly allowance resets.")


def run_source(name, cfg, st):
    s = st.setdefault(name, {})
    today = date.today()
    covered = s.get("covered")  # only moves on after a complete run, so a failed or cut-short one doesn't lose roles
    max_age = int(cfg.get("sources_max_age_days", 14))
    age = min(max_age, (today - date.fromisoformat(covered)).days + OVERLAP) if covered else max_age
    run = collect(name, cfg, st, budget(name, cfg, st), age)
    rows = run["rows"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with locked(JOBS) as store:
        for jid in rows.keys() & store.keys():
            store[jid]["last_seen"] = now
        kept, dropped = clean(rows.values(), store, cfg, now=now)
        for j in kept:
            j.update(first_seen=now, last_seen=now, stage="new", score=None, why=None, source=name)
            store[j["id"]] = j
        close_old(store, cfg, now)
    notes = [f"{len(rows)} found, {len(kept)} new, {run['spent']} requests"]
    if run["empty"]:
        notes.append(f"no results for {run['empty']} search{'es' if run['empty'] > 1 else ''}")
    if run["faults"]:
        notes.append(f"{run['faults']} failed")
    if run["stopped"]:
        notes.append(run["stopped"])
    if not (run["stopped"] or run["faults"] or run["cut"]):
        s["covered"] = str(today)
    s.update(last_run=str(today), last_result="; ".join(notes), last_dropped=dropped, cut=run["cut"])
    save_state(st)
    return s["last_result"]


def close_old(store, cfg, now) -> None:
    """These sources can't say when an ad has gone, so roles nobody acted on are closed once they're this old."""
    cutoff = str(date.today() - timedelta(days=int(cfg.get("sources_close_after_days", 45))))
    for j in store.values():
        if j.get("source") in LABEL and j.get("stage") == "new" and not j.get("ad_closed") and str(j.get("posted"))[:10] < cutoff:
            j["ad_closed"] = now


def check_limits(name, cfg, st) -> None:
    full = next((r for r in limits(name, cfg, st) if r["left"] <= 0), None)
    if full:
        tell_limit(name, st, full)


def run_one(name, cfg, st) -> str:
    label = LABEL[name]
    confirmed = name != "jsearch" or refresh_quota(st)
    check_limits(name, cfg, st)
    if not is_due(name, cfg, st):
        last = date.fromisoformat(st[name]["last_run"])
        return f"{label}: next due {nice(last + timedelta(days=int(cfg.get(f'{name}_every_days', 7))))}"
    if not confirmed:
        return f"{label}: couldn't confirm the allowance, so nothing was requested"
    if budget(name, cfg, st) <= 0:
        full = next((r for r in limits(name, cfg, st) if r["left"] <= 0), None)
        return f"{label}: limit reached, resets {nice(full['resets'])}" if full else f"{label}: no allowance left"
    result = run_source(name, cfg, st)
    if name == "jsearch":
        refresh_quota(st)  # so Health shows what this run used
    check_limits(name, cfg, st)
    return f"{label}: {result}"


def main():
    cfg, st = load_config(), load_state()
    for name in LABEL:
        if not cfg.get(f"source_{name}"):
            print(f"{LABEL[name]}: turned off")
        elif not all(secret(k) for k in KEYS[name]):
            print(f"{LABEL[name]}: key missing")
        else:
            try:
                print(run_one(name, cfg, st))
            except Exception as e:  # one source's fault shouldn't stop the other. The type only: details could hold a key
                print(f"{LABEL[name]}: had a problem ({type(e).__name__})")


def show():
    for s in status():
        print(f"{s['label']}: {'on' if s['on'] else 'off'}, key {'set' if s['has_key'] else 'missing'}, last run {s['last_run'] or 'never'}")
        print(f"  {s['plan']}")
        for r in s["limits"]:
            print(f"  {r['period']}: {r['used']} used of {r['limit']}, {r['left']} left, resets {r['resets']}{'' if r['exact'] else ' (estimate)'}")
        for w in s["warnings"]:
            print(f"  ! {w}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["status"]:
        show()
    elif sys.argv[1:2] == ["record"] and len(sys.argv) >= 4 and sys.argv[2] in LABEL:
        spend(load_state(), sys.argv[2], int(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else None)
        show()
    elif sys.argv[1:]:
        sys.exit(__doc__)
    else:
        main()
