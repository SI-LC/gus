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

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from radar.scan import DATA, JOBS, UA, load_config, matches, strip_html
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
    except (OSError, ValueError):
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
            used = used_in(p, s.get("days", {}), today)
            rows.append({"period": p, "limit": limit, "used": used, "left": max(0, limit - used),
                         "resets": str(resets(p, today)), "exact": False})
        return rows
    q = s.get("quota")
    if not q:
        return []
    return [{"period": "month", "limit": q["limit"], "used": q["used"], "left": q["remaining"],
             "resets": q["reset_at"][:10], "exact": True}]


def budget(name, cfg, st) -> int:
    """How many requests this run may make. No confirmed figure means none, so a fault can't lead to overspending."""
    rows = limits(name, cfg, st)
    run = int(cfg.get(f"{name}_requests_per_run", 0))
    if name == "jsearch":
        q = st.get(name, {}).get("quota") or {}
        if q.get("checked") != str(date.today()):
            return 0
        return max(0, min(run, q["remaining"] - int(cfg.get("jsearch_reserve", 0))))
    return max(0, min([run] + [r["left"] for r in rows]))


def is_due(name, cfg, st, today=None) -> bool:
    last = st.get(name, {}).get("last_run")
    return not last or ((today or date.today()) - date.fromisoformat(last)).days >= int(cfg.get(f"{name}_every_days", 7))


def refresh_quota(st) -> bool:
    """Ask JSearch how much is left. This doesn't use up a request."""
    url = JSEARCH_USAGE + "?" + urllib.parse.urlencode({"api_id": "jsearch"})
    try:
        q = call(url, {"x-api-key": secret("jsearch_api_key")})["data"]["quotas"][0]
    except (Failed, KeyError, IndexError, TypeError):
        return False
    st.setdefault("jsearch", {})["quota"] = {"limit": q["limit"], "used": q["used"], "remaining": q["remaining"],
                                             "reset_at": q["reset_at"], "checked": str(date.today())}
    save_state(st)
    return True


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
                    "last_run": last, "next_run": next_run, "last_result": s.get("last_result", "")})
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
                    "title": (r.get("title") or "").strip(), "url": r.get("redirect_url"), "posted": r.get("created"),
                    "location": ", ".join(filter(None, [loc.get("display_name"), country])),
                    "description": strip_html(r.get("description") or "")})
    return out


def jsearch(term, page, cfg, age):
    place = cfg.get("sources_location") or {"gb": "United Kingdom"}.get(cfg.get("sources_country", "gb"), "")
    window = "today" if age <= 1 else "3days" if age <= 3 else "week" if age <= 7 else "month"
    q = {"query": f"{term} in {place}".strip(), "country": cfg.get("sources_country", "gb"), "date_posted": window, "page": page, "num_pages": 1}
    d = call(JSEARCH + "?" + urllib.parse.urlencode(q), {"x-api-key": secret("jsearch_api_key")})
    data = d.get("data")
    country = "UK" if cfg.get("sources_country", "gb") == "gb" else cfg.get("sources_country", "").upper()
    out = []
    for r in (data if isinstance(data, list) else (data or {}).get("jobs", [])):
        place_name = re.sub(r"\s+", " ", r.get("job_location") or "").strip()
        place_name = "Remote" if r.get("job_is_remote") or place_name == "Anywhere" else place_name
        out.append({"id": f"jsearch:{r['job_id']}", "company": (r.get("employer_name") or "").strip(), "title": (r.get("job_title") or "").strip(),
                    "url": r.get("job_apply_link"), "posted": r.get("job_posted_at_datetime_utc"),
                    "location": ", ".join(filter(None, [place_name, country])), "description": r.get("job_description") or ""})
    return out


FETCH = {"adzuna": adzuna, "jsearch": jsearch}


# --- the same clean-up for every source -----------------------------------------

def same_company(s) -> str:
    s = re.sub(r"[(),.'’]", " ", (s or "").lower().replace("&", "and"))
    s = re.sub(r"\b(limited|ltd|plc|inc|llc|llp|sarl|gmbh|group|holdings?|uk|europe|emea|careers|company|the|corp|corporation)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def same_title(s) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", (s or "").lower())
    s = re.sub(r"\bjob in .*$|\s[-–—|]\s*(london|uk|remote|hybrid|united kingdom).*$", " ", s)
    s = re.sub(r"\bsr\b\.?", "senior", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def hits(pattern, text) -> bool:
    return bool(pattern and pattern.pattern and pattern.search(text))  # an empty setting must not match everything


def clean(rows, store, cfg, today=None):
    """Keep real, fresh, wanted roles that Gus doesn't already have. Returns the roles and why others went."""
    cutoff = str((today or date.today()) - timedelta(days=int(cfg.get("sources_max_age_days", 14))))
    seen = {(same_company(j.get("company")), same_title(j.get("title"))) for j in store.values()}
    kept, dropped = [], dict.fromkeys(("old", "no employer", "agency", "repeat", "not a match"), 0)
    for j in rows:
        key = (same_company(j["company"]), same_title(j["title"]))
        if j["id"] in store:
            continue
        if not j.get("posted") or str(j["posted"])[:10] < cutoff:
            why = "old"
        elif not key[0]:
            why = "no employer"
        elif hits(cfg.get("source_company_exclude"), j["company"]) or hits(cfg.get("source_text_exclude"), j["description"][:6000]):
            why = "agency"
        elif key in seen:
            why = "repeat"
        elif not matches(j, cfg):
            why = "not a match"
        else:
            seen.add(key)
            kept.append(j)
            continue
        dropped[why] += 1
    return kept, dropped


# --- a run -----------------------------------------------------------------------

def collect(name, cfg, st, allowed, age):
    """Spend at most `allowed` requests. Stops a source on a refusal; a passing fault only skips that search."""
    rows, spent, empty, faults, stopped = {}, 0, [], 0, ""
    started = time.monotonic()
    terms = [t.strip() for t in str(cfg.get("sources_searches", "")).splitlines() if t.strip()]
    for term in terms:
        for page in range(1, int(cfg.get("sources_pages_per_search", 3)) + 1):
            if spent >= allowed:
                return rows, spent, empty, faults, f"stopped at the {allowed} request limit"
            if time.monotonic() - started > TIME_LIMIT:
                return rows, spent, empty, faults, "stopped: taking too long"
            spend(st, name)  # before the call: a request that times out may still have counted
            spent += 1
            try:
                found = FETCH[name](term, page, cfg, age)
            except Failed as e:
                if e.code in REFUSED:
                    return rows, spent, empty, faults, f"refused ({e})"
                faults += 1
                break
            fresh = [j for j in found if j["id"] not in rows]
            rows.update((j["id"], j) for j in fresh)
            if page == 1 and not found:
                empty.append(term)
            if len(found) < FULL_PAGE[name] or len(fresh) < 3:
                break
    return rows, spent, empty, faults, stopped


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
    last = s.get("last_run")
    max_age = int(cfg.get("sources_max_age_days", 14))
    age = min(max_age, (today - date.fromisoformat(last)).days + 2) if last else max_age
    rows, spent, empty, faults, stopped = collect(name, cfg, st, budget(name, cfg, st), age)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with locked(JOBS) as store:
        for jid in rows.keys() & store.keys():
            store[jid]["last_seen"] = now
        kept, dropped = clean(rows.values(), store, cfg)
        for j in kept:
            j.update(first_seen=now, last_seen=now, stage="new", score=None, why=None, source=name)
            store[j["id"]] = j
    notes = [f"{len(rows)} found, {len(kept)} new, {spent} requests"]
    if empty:
        notes.append(f"no results for {len(empty)} search{'es' if len(empty) > 1 else ''}")
    if faults:
        notes.append(f"{faults} failed")
    if stopped:
        notes.append(stopped)
    s.update(last_run=str(today), last_result="; ".join(notes), last_dropped=dropped)
    save_state(st)
    return notes[0] if len(notes) == 1 else "; ".join(notes)


def main():
    cfg, st = load_config(), load_state()
    for name in LABEL:
        label = LABEL[name]
        if not cfg.get(f"source_{name}"):
            print(f"{label}: turned off in settings")
            continue
        if not all(secret(k) for k in KEYS[name]):
            print(f"{label}: no key set (Settings → Keys); skipping")
            continue
        confirmed = name != "jsearch" or refresh_quota(st)
        at_limit = next((r for r in limits(name, cfg, st) if r["left"] <= 0), None)
        if at_limit:
            tell_limit(name, st, at_limit)
        if not is_due(name, cfg, st):
            print(f"{label}: not due yet")
        elif not confirmed:
            print(f"{label}: couldn't confirm the allowance, so nothing was requested")
        elif budget(name, cfg, st) <= 0:
            print(f"{label}: no allowance left")
        else:
            print(f"{label}: {run_source(name, cfg, st)}")


def show():
    for s in status():
        print(f"{s['label']}: {'on' if s['on'] else 'off'}, key {'set' if s['has_key'] else 'missing'}, last run {s['last_run'] or 'never'}")
        for r in s["limits"]:
            print(f"  {r['period']}: {r['used']} used of {r['limit']}, {r['left']} left, resets {r['resets']}{'' if r['exact'] else ' (estimate)'}")


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
