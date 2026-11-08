"""Account-based job search: accounts (companies), the people mapped at each,
dated events that make an account worth acting on, and the weekly moves.

data/companies.json   accounts: tier, fit, research
data/events.json      dated, linked things that happened at an account (radar.events)
data/people.json      people per account: role, kind, evidence, warm path, stage
data/moves.json       weekly moves: one person + one fresh event + angle + draft

A move is only accepted if it cites an event from the last FRESH_DAYS days at an A or B
account, to a person mapped there with evidence. That's enforced here, not left to the
agent's judgement.

    python3 -m radar.accounts migrate              # one-off: people from roles → people.json, tiers
    python3 -m radar.accounts brief                # what the planner sees: fresh events by account
    python3 -m radar.accounts plan FILE.json       # validate proposed moves, keep the best 5
    python3 -m radar.accounts funnel               # events → moves → replies → conversations
    python3 -m radar.accounts add-person "Name" "Company" [--role R] [--url U] [--kind K]
                                                   # someone you know of, e.g. a recruiter who contacted you
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone

from radar import events as E
from radar.events import EVENTS, FRESH_DAYS, parse_date
from radar.scan import DATA, JOBS
from radar.store import locked, read

COMPANIES = DATA / "companies.json"
PEOPLE = DATA / "people.json"
MOVES = DATA / "moves.json"
MAX_MOVES = 5
COOLDOWN_DAYS = 21  # don't contact the same account again this soon without a new signal

EVENT_LABEL = E.LABEL
KINDS = ["product_leader", "hiring_manager", "peer_pm", "recruiter", "warm", "announcer"]
PERSON_STAGES = ["mapped", "contacted", "replied", "conversation", "referred", "closed"]
OUTCOMES = ["no_reply", "replied", "conversation", "interview", "referral", "not_now"]


def today():
    return date.today()


def week_id(d=None):
    y, w, _ = (d or today()).isocalendar()
    return f"{y}-W{w:02d}"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def person_id(name, company):
    return f"{slug(name)}@{slug(company)}"



def tier(c: dict) -> str:
    if c.get("tier") in ("A", "B", "C"):
        return c["tier"]
    if c.get("status") not in ("watched", "maybe"):
        return "-"
    fit = c.get("fit")
    if fit is None:
        return "B" if c.get("status") == "watched" else "C"
    return "A" if fit >= 8 else "B" if fit >= 7 else "C"


def events(name: str, c: dict | None = None, jobs: dict | None = None, stored: dict | None = None) -> list[dict]:
    """An account's events from data/events.json, fresh and strong first. `c` and `jobs` are accepted for older callers."""
    return E.for_account(name, read(EVENTS, {}) if stored is None else stored)


def last_touch(account: str, moves: dict):
    done = [parse_date(m.get("done_at")) for m in moves.values() if m.get("account") == account and m.get("status") == "done"]
    done = [d for d in done if d]
    return max(done) if done else None


def brief() -> dict:
    """Everything the weekly planner needs, already filtered to what's actionable."""
    from radar.network import distances
    E.sync()
    companies, people, moves, stored = read(COMPANIES, {}), read(PEOPLE, {}), read(MOVES, {}), read(EVENTS, {})
    dist = distances(people)
    out = []
    for name, c in companies.items():
        t = tier(c)
        if t not in ("A", "B"):
            continue
        fresh = [e for e in events(name, stored=stored) if e["fresh"]]
        if not fresh:
            continue
        touched = last_touch(name, moves)
        ppl = [dict(p, id=pid) for pid, p in people.items() if p.get("company") == name and p.get("stage") != "closed"]
        out.append({
            "account": name, "tier": t, "fit": c.get("fit"), "why_it_fits": c.get("fit_why"),
            "fresh_events": fresh[:5],
            "people": [{**{k: p.get(k) for k in ("id", "name", "role", "kind", "warm", "evidence", "url", "stage", "confidence")},
                        "distance": dist[p["id"]]} for p in ppl],
            "last_contacted": str(touched) if touched else None,
            "cooling_down": bool(touched and (today() - touched).days < COOLDOWN_DAYS),
        })
    out.sort(key=lambda a: (a["tier"], -max(e["strength"] for e in a["fresh_events"]), -(a["fit"] or 0)))
    return {"week": week_id(), "accounts": out}


def resolve_event(m: dict, stored: dict) -> dict | None:
    """The stored event a proposed move points at: by `event_id`, or by the url of an older `signal` dict."""
    if m.get("event_id") in stored:
        return dict(stored[m["event_id"]], id=m["event_id"])
    url = (m.get("signal") or {}).get("url")
    eid = E.by_url(stored, m.get("account"), url) if url else None
    return dict(stored[eid], id=eid) if eid else None


def validate(m: dict, companies, people, stored, moves) -> list[str]:
    errs = []
    acct = m.get("account")
    c = companies.get(acct)
    if not c:
        return [f"unknown account {acct!r}"]
    if tier(c) not in ("A", "B"):
        errs.append(f"{acct} is tier {tier(c)}, not A or B")
    p = people.get(m.get("person_id") or "")
    if not p or p.get("company") != acct:
        errs.append("person isn't mapped at this account")
    elif not (p.get("url") and (p.get("evidence") or p.get("warm"))):
        errs.append("person has no evidence link")
    ev = resolve_event(m, stored)
    d = parse_date(ev.get("date")) if ev else None
    if not ev:
        errs.append("event_id isn't one of this account's recorded events (record it in data/events.json first)")
    elif ev.get("account") != acct:
        errs.append(f"event belongs to {ev.get('account')!r}, not {acct!r}")
    elif ev.get("closed"):
        errs.append("that role has closed")
    elif not d:
        errs.append("event has no date")
    elif (today() - d).days > FRESH_DAYS:
        errs.append(f"event is {(today() - d).days} days old (limit {FRESH_DAYS})")
    touched = last_touch(acct, moves)
    if touched and (today() - touched).days < COOLDOWN_DAYS and not (d and d > touched):
        errs.append(f"contacted {(today() - touched).days} days ago and nothing new since")
    for k in ("why_now", "angle", "draft"):
        if not (m.get(k) or "").strip():
            errs.append(f"missing {k}")
    return errs


def plan(path) -> None:
    proposed = json.loads(open(path).read())
    E.sync()
    companies, people, stored = read(COMPANIES, {}), read(PEOPLE, {}), read(EVENTS, {})
    wk = week_id()
    with locked(MOVES) as moves:
        open_this_week = [m for m in moves.values() if m.get("week") == wk and m.get("status") == "todo"]
        room = MAX_MOVES - len(open_this_week)
        used_accounts = {m["account"] for m in open_this_week}
        kept = 0
        for m in proposed:
            errs = validate(m, companies, people, stored, moves)
            if m.get("account") in used_accounts:
                errs.append("already a move for this account this week")
            if errs:
                print(f"  ✗ {m.get('account')} / {m.get('person_id')}: " + "; ".join(errs))
                continue
            if kept >= room:
                print(f"  – {m['account']}: valid, but this week already has {MAX_MOVES} moves")
                continue
            ev = resolve_event(m, stored)
            mid = f"{wk}:{m['person_id']}"
            moves[mid] = {
                "id": mid, "week": wk, "account": m["account"], "person_id": m["person_id"], "event_id": ev["id"],
                "why_now": m["why_now"].strip(), "angle": m["angle"].strip(), "draft": m["draft"].strip(),
                "channel": m.get("channel") or people[m["person_id"]].get("platform") or "linkedin",
                "give_first": (m.get("give_first") or "").strip(),
                "status": "todo", "outcome": None, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            used_accounts.add(m["account"])
            kept += 1
            print(f"  ✓ {m['account']} → {people[m['person_id']]['name']}: {ev.get('summary', '')[:70]}")
    print(f"{kept} moves added for {wk}")


def funnel(days=90) -> dict:
    since = today() - timedelta(days=days)
    moves = [m for m in read(MOVES, {}).values() if (parse_date(m.get("created")) or today()) >= since]
    jobs, companies, stored = read(JOBS, {}), read(COMPANIES, {}), read(EVENTS, {})
    fresh = sum(1 for n, c in companies.items() if tier(c) in ("A", "B") for e in events(n, stored=stored) if e["fresh"])
    reached = {"replied", "conversation", "interview", "referral"}
    conv = {"conversation", "interview", "referral"}
    return {
        "signals": fresh,
        "moves": len(moves),
        "done": sum(m["status"] == "done" for m in moves),
        "replied": sum(m.get("outcome") in reached for m in moves),
        "conversations": sum(m.get("outcome") in conv for m in moves),
        "interviews": sum(j.get("stage") == "interviewing" for j in jobs.values()) + sum(m.get("outcome") == "interview" for m in moves),
    }


def migrate():
    """One-off: people found per role become account people; accounts get tiers."""
    jobs = read(JOBS, {})
    kind_map = {"hiring_manager": "hiring_manager", "announcer": "announcer", "recruiter": "recruiter"}
    added = 0
    with locked(PEOPLE) as people:
        for j in jobs.values():
            for p in j.get("people") or []:
                if not p.get("name") or not p.get("url"):
                    continue
                pid = person_id(p["name"], j["company"])
                rec = people.setdefault(pid, {
                    "name": p["name"], "company": j["company"], "role": p.get("role"),
                    "kind": kind_map.get(p.get("kind"), "product_leader"), "platform": p.get("platform"),
                    "url": p["url"], "evidence": p.get("evidence"), "confidence": p.get("confidence"),
                    "warm": False, "stage": "contacted" if p.get("sent_at") else "mapped",
                    "added": str(today()), "source": "role people search",
                })
                rec.setdefault("job_ids", [])
                if j["id"] not in rec["job_ids"]:
                    rec["job_ids"].append(j["id"])
                added += 1
    print(f"{added} people links from roles → {len(read(PEOPLE, {}))} people")


def add_person(name: str, company: str, role: str = "", url: str = "", kind: str = "", evidence: str = "") -> tuple[str, bool]:
    """Record a person by hand (an inbound recruiter, someone you've met). Returns (id, created).
    An existing person is left as they are, apart from filling in fields they don't have yet."""
    name, company = (name or "").strip(), (company or "").strip()
    if not name or not company:
        raise ValueError("Needs a name and a company")
    known = {c.lower(): c for c in read(COMPANIES, {})}
    if company.lower() not in known:
        raise ValueError(f"{company} isn't a tracked account yet")
    company = known[company.lower()]
    if kind and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if url and not re.match(r"https?://", url):
        raise ValueError("url must be a web link")
    pid = person_id(name, company)
    if not re.fullmatch(r"[a-z0-9-]+@[a-z0-9-]+", pid):
        raise ValueError("Name and company need letters or numbers")
    with locked(PEOPLE) as people:
        created = pid not in people
        p = people.setdefault(pid, {"name": name, "company": company, "stage": "mapped", "added": str(today()),
                                    "warm": False, "source": "manual",
                                    "history": [{"stage": "mapped", "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}]})
        for k, v in (("role", role), ("url", url), ("kind", kind), ("evidence", evidence)):
            if v and not p.get(k):
                p[k] = v.strip()[:2000]
    return pid, created


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "brief"
    if cmd == "migrate":
        migrate()
    elif cmd == "brief":
        print(json.dumps(brief(), indent=2, ensure_ascii=False))
    elif cmd == "plan":
        plan(sys.argv[2])
    elif cmd == "funnel":
        print(json.dumps(funnel(), indent=2))
    elif cmd == "add-person":
        import argparse
        ap = argparse.ArgumentParser(prog="radar.accounts add-person")
        ap.add_argument("name"), ap.add_argument("company")
        for f in ("role", "url", "kind", "evidence"):
            ap.add_argument(f"--{f}", default="")
        a = ap.parse_args(sys.argv[2:])
        try:
            pid, created = add_person(a.name, a.company, a.role, a.url, a.kind, a.evidence)
        except ValueError as e:
            sys.exit(str(e))
        print(f"{'added' if created else 'already there'}: {pid}")


if __name__ == "__main__":
    main()
