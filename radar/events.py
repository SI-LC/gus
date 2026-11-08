"""Events: the dated things that happened at an account which make it worth acting on now.
data/events.json, one record per event, keyed by id. A move is built on one of these.

An event comes from a signal the discover agent promoted, from the watchlist agent's research, from a role
that scored ROLE_EVENT_SCORE or more, or from you. Every one needs a link and a real date.

    python3 -m radar.events sync          # housekeeping: role events from jobs, migrate older shapes
    python3 -m radar.events alert         # push fresh hiring news at A/B accounts to your phone, once each
    python3 -m radar.events list "Acme"   # an account's events, fresh first

`sync` runs on every scan and before the planner reads. It's idempotent:
- a role scoring 7+ that's still open has a `role_posted` event (`role:<job id>`); when the ad closes or
  you skip the role, the event gets `closed` and stops counting
- events an older agent wrote inline on an account (`companies.json` → `events`) move here, and an account's
  `pending_signals` become `company` on the signals themselves
- a move still carrying a copied `signal` dict gets an `event_id` instead
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta

from radar.scan import DATA, JOBS
from radar.store import locked, read

EVENTS = DATA / "events.json"
COMPANIES = DATA / "companies.json"
MOVES = DATA / "moves.json"
SIGNALS = DATA / "signals.json"

ROLE_EVENT_SCORE = 7  # the one place the "a role this good is a signal" threshold lives
FRESH_DAYS = 30

TYPES = ["post_hiring", "role_posted", "leader_hire", "funding", "london_office", "launch", "mention"]
# how strongly each kind of event says "act now"
STRENGTH = {"post_hiring": 5, "role_posted": 4, "leader_hire": 4, "funding": 3, "london_office": 3, "launch": 2, "mention": 1}
LABEL = {"post_hiring": "Posted about hiring", "role_posted": "Role posted", "leader_hire": "New product leader",
         "funding": "Funding", "london_office": "London office", "launch": "Launch in your area", "mention": "In the news"}
FIELDS = ["account", "type", "summary", "url", "date"]
ALERT_TYPES = ["post_hiring", "leader_hire", "role_posted"]  # the news that most often means a role is coming
ALERT_WINDOW_DAYS = 2  # only news recorded this recently is pushed, so a backlog never floods the phone
ALERT_MAX = 5  # pushes per run; the rest go in one summary


def today():
    return date.today()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def parse_date(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s / 1000 if s > 1e11 else s).date()
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", str(s))
        return date(int(m[1]), int(m[2]), int(m[3] or 1)) if m else None



def event_id(account: str, type_: str, when, existing: dict | None = None, url: str | None = None) -> str:
    """`<account slug>:<type>:<date>`, with `-2`, `-3`… when that day already has one about something else."""
    base = f"{slug(account)}:{type_}:{parse_date(when) or 'undated'}"
    if not existing:
        return base
    for n in range(1, 50):
        eid = base if n == 1 else f"{base}-{n}"
        if eid not in existing or (url and existing[eid].get("url") == url):
            return eid
    return f"{base}-{n + 1}"


def by_url(events: dict, account: str, url: str) -> str | None:
    return next((eid for eid, e in events.items() if e.get("account") == account and e.get("url") == url), None)


def add(account: str, type_: str, summary: str, url: str, when: str, source: str = "manual") -> str:
    """Record an event by hand or from code; the same link at the same account isn't recorded twice."""
    if type_ not in TYPES:
        raise ValueError(f"type must be one of {TYPES}")
    if not (url and re.match(r"https?://", url)):
        raise ValueError("needs a web link")
    if not parse_date(when):
        raise ValueError("needs a real date (YYYY-MM-DD)")
    with locked(EVENTS) as events:
        eid = by_url(events, account, url) or event_id(account, type_, when, events, url)
        events.setdefault(eid, {"account": account, "type": type_, "summary": (summary or "").strip()[:300], "url": url,
                               "date": str(parse_date(when)), "source": source, "added": str(today())})
    return eid


def decorate(e: dict) -> dict:
    """Age, freshness and strength, worked out at read time."""
    d = parse_date(e.get("date"))
    e["age_days"] = (today() - d).days if d else None
    e["fresh"] = d is not None and e["age_days"] <= FRESH_DAYS and not e.get("closed")
    e["strength"] = STRENGTH.get(e.get("type"), 1)
    return e


def for_account(name: str, events: dict) -> list[dict]:
    """An account's events with age, freshness and strength, fresh and strong first. Closed role events are left out."""
    ev = [decorate(dict(e, id=eid)) for eid, e in events.items()
          if e.get("account") == name and e.get("url") and not e.get("closed")]
    return sorted(ev, key=lambda e: (not e["fresh"], -e["strength"], e["age_days"] if e["age_days"] is not None else 999))


def sync(jobs: dict | None = None) -> dict:
    """Housekeeping, safe to run any time. Returns counts of what it changed."""
    jobs = read(JOBS, {}) if jobs is None else jobs
    out = {"migrated": 0, "role_events": 0, "closed": 0, "moves": 0, "signals": 0}
    with locked(EVENTS) as events:
        # 1. events an older agent wrote inline on the account, and the signals it was flagged for
        with locked(COMPANIES) as companies, locked(SIGNALS) as signals:
            by_link = {s.get("url"): s for s in signals.values() if s.get("url")}
            for name, c in companies.items():
                for e in c.pop("events", None) or []:
                    if not isinstance(e, dict) or not e.get("url"):
                        continue
                    eid = by_url(events, name, e["url"]) or event_id(name, e.get("type") or "mention", e.get("date"), events, e["url"])
                    if eid not in events:
                        events[eid] = {"account": name, "type": e.get("type") if e.get("type") in TYPES else "mention",
                                       "summary": (e.get("summary") or "")[:300], "url": e["url"],
                                       "date": str(parse_date(e.get("date")) or ""), "source": "account", "added": str(today())}
                        out["migrated"] += 1
                for ps in c.pop("pending_signals", None) or []:  # the signal itself now carries `company`; the account keeps `reassess`
                    lead = by_link.get(ps.get("url") if isinstance(ps, dict) else None)
                    if lead is not None and not lead.get("company"):
                        lead["company"] = name
                        out["signals"] += 1
            accounts = set(companies)
        # 2. role events: a role this good at an account is a signal while it's open
        for jid, j in jobs.items():
            good = (j.get("score") or 0) >= ROLE_EVENT_SCORE and j.get("company") in accounts
            live = good and not j.get("ad_closed") and j.get("stage") not in ("skipped", "closed")
            eid = f"role:{jid}"
            if not good and eid in events and j.get("company") not in accounts:
                del events[eid]  # a role at a company that isn't an account was never a signal
                out["dropped"] = out.get("dropped", 0) + 1
            elif live:
                posted = parse_date(j.get("posted")) or parse_date(j.get("first_seen"))
                if eid not in events:
                    events[eid] = {"account": j["company"], "type": "role_posted", "url": j.get("url"), "job_id": jid,
                                   "source": f"job:{jid}", "added": str(today())}
                    out["role_events"] += 1
                events[eid].update(summary=f"{j.get('title')} ({j['score']}/10)", date=str(posted) if posted else None)
                if events[eid].pop("closed", None):
                    out["role_events"] += 1
            elif eid in events and not events[eid].get("closed"):
                events[eid]["closed"] = j.get("ad_closed") or str(today())
                out["closed"] += 1
        # 3. moves that still carry a copied signal
        with locked(MOVES) as moves:
            for m in moves.values():
                sig = m.get("signal")
                if m.get("event_id") or not isinstance(sig, dict):
                    continue
                if sig.get("url"):
                    eid = by_url(events, m.get("account"), sig["url"]) or event_id(m.get("account"), sig.get("type") or "mention", sig.get("date"), events, sig["url"])
                    events.setdefault(eid, {"account": m.get("account"), "type": sig.get("type") if sig.get("type") in TYPES else "mention",
                                            "summary": (sig.get("summary") or "")[:300], "url": sig["url"],
                                            "date": str(parse_date(sig.get("date")) or ""), "source": "move", "added": str(today())})
                    m["event_id"] = eid
                    m.pop("signal", None)
                    out["moves"] += 1
    return out


def alert(stored: dict | None = None, jobs: dict | None = None) -> list[str]:
    """Push a phone alert for fresh hiring news at an A or B account, once per event. Returns the ids alerted.
    A role event whose role already earned a role alert (score at or above `notify_min_score`) isn't pushed again."""
    from radar import accounts as A
    from radar.notify import notify
    from radar.settings import load as load_settings
    cfg = load_settings()
    if not cfg.get("notify_news", True):
        return []
    role_threshold = cfg.get("notify_min_score") or 10
    companies = read(COMPANIES, {})
    jobs = read(JOBS, {}) if jobs is None else jobs
    since = str(today() - timedelta(days=ALERT_WINDOW_DAYS))
    sent = []
    with locked(EVENTS) as events:
        for eid, e in events.items():
            if e.get("notified") or e.get("closed") or e.get("type") not in ALERT_TYPES or (e.get("added") or "") < since:
                continue
            if A.tier(companies.get(e.get("account")) or {}) not in ("A", "B") or not decorate(dict(e))["fresh"]:
                continue
            if e.get("type") == "role_posted" and (jobs.get(e.get("job_id") or "", {}).get("score") or 0) >= role_threshold:
                e["notified"] = "role alert"  # triage already pushed this one
                continue
            e["notified"] = str(today())
            sent.append(eid)
            if len(sent) <= ALERT_MAX:
                notify(f"{LABEL.get(e['type'], e['type'])} · {e['account']}", "", (e.get("summary") or "")[:300], e.get("url") or "")
        if len(sent) > ALERT_MAX:
            more = [events[i]["account"] for i in sent[ALERT_MAX:]]
            notify(f"Gus: {len(more)} more hiring signals", "", ", ".join(dict.fromkeys(more))[:900])
    return sent


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "sync":
        print(json.dumps(sync()))
    elif cmd == "alert":
        print(json.dumps(alert()))
    elif cmd == "list":
        for e in for_account(sys.argv[2], read(EVENTS, {})):
            print(f"{'●' if e['fresh'] else '○'} {e['date']}  {e['type']:<14} {e.get('summary', '')[:70]}  {e['url']}")
    elif cmd == "add":
        import argparse
        ap = argparse.ArgumentParser(prog="radar.events add")
        for f in ("account", "type", "summary", "url", "date"):
            ap.add_argument(f)
        a = ap.parse_args(sys.argv[2:])
        try:
            print(add(a.account, a.type, a.summary, a.url, a.date))
        except ValueError as e:
            sys.exit(str(e))


if __name__ == "__main__":
    main()
