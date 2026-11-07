"""Account-based job search: accounts (companies), the people mapped at each,
dated events that make an account worth acting on, and the weekly moves.

data/companies.json   accounts: tier, fit, events [{type, summary, url, date}]
data/people.json      people per account: role, kind, evidence, warm path, stage
data/moves.json       weekly moves: one person + one fresh signal + angle + draft

A move is only accepted if it cites a dated signal from the last FRESH_DAYS days
with a link, at an A or B account, to a person mapped there with evidence. That's
enforced here, not left to the agent's judgement.

    python3 -m radar.accounts migrate              # one-off: people from roles → people.json, tiers
    python3 -m radar.accounts brief                # what the planner sees: fresh signals by account
    python3 -m radar.accounts plan FILE.json       # validate proposed moves, keep the best 5
    python3 -m radar.accounts funnel               # signals → moves → replies → conversations
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone

from radar.scan import DATA, JOBS
from radar.store import locked

COMPANIES = DATA / "companies.json"
PEOPLE = DATA / "people.json"
MOVES = DATA / "moves.json"
FRESH_DAYS = 30
MAX_MOVES = 5
COOLDOWN_DAYS = 21  # don't contact the same account again this soon without a new signal

# how strongly each kind of event says "act now"
STRENGTH = {"post_hiring": 5, "role_posted": 4, "leader_hire": 4, "funding": 3, "london_office": 3,
            "launch": 2, "mention": 1}
EVENT_LABEL = {"post_hiring": "Posted about hiring", "role_posted": "Role posted", "leader_hire": "New product leader",
               "funding": "Funding", "london_office": "London office", "launch": "Launch in your area", "mention": "In the news"}
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


def parse_date(s):
    if not s:
        return None
    if isinstance(s, (int, float)):  # epoch seconds (Reddit) or milliseconds
        return datetime.fromtimestamp(s / 1000 if s > 1e11 else s, timezone.utc).date()
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", str(s))
        return date(int(m[1]), int(m[2]), int(m[3] or 1)) if m else None


def read(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def tier(c: dict) -> str:
    if c.get("tier") in ("A", "B", "C"):
        return c["tier"]
    if c.get("status") not in ("watched", "maybe"):
        return "-"
    fit = c.get("fit")
    if fit is None:
        return "B" if c.get("status") == "watched" else "C"
    return "A" if fit >= 8 else "B" if fit >= 7 else "C"


def events(name: str, c: dict, jobs: dict) -> list[dict]:
    """Stored events plus live roles scoring 7+ (derived, so they're always current)."""
    ev = [dict(e) for e in c.get("events", []) if e.get("url")]
    for j in jobs.values():
        if j.get("company") == name and (j.get("score") or 0) >= 7 and not j.get("ad_closed") and j.get("stage") not in ("skipped", "closed"):
            # the company's own posting date; when we first saw it only if the board doesn't say
            posted = parse_date(j.get("posted")) or parse_date(j.get("first_seen"))
            ev.append({"type": "role_posted", "summary": f"{j['title']} ({j['score']}/10)", "url": j["url"],
                       "date": str(posted) if posted else None, "job_id": j["id"]})
    for e in ev:
        d = parse_date(e.get("date"))
        e["age_days"] = (today() - d).days if d else None
        e["fresh"] = d is not None and e["age_days"] <= FRESH_DAYS
        e["strength"] = STRENGTH.get(e.get("type"), 1)
    return sorted(ev, key=lambda e: (not e["fresh"], -e["strength"], e["age_days"] if e["age_days"] is not None else 999))


def last_touch(account: str, moves: dict):
    done = [parse_date(m.get("done_at")) for m in moves.values() if m.get("account") == account and m.get("status") == "done"]
    done = [d for d in done if d]
    return max(done) if done else None


def brief() -> dict:
    """Everything the weekly planner needs, already filtered to what's actionable."""
    from radar.network import distances
    companies, people, jobs, moves = read(COMPANIES, {}), read(PEOPLE, {}), read(JOBS, {}), read(MOVES, {})
    dist = distances(people)
    out = []
    for name, c in companies.items():
        t = tier(c)
        if t not in ("A", "B"):
            continue
        fresh = [e for e in events(name, c, jobs) if e["fresh"]]
        if not fresh:
            continue
        touched = last_touch(name, moves)
        ppl = [dict(p, id=pid) for pid, p in people.items() if p.get("company") == name and p.get("stage") != "closed"]
        out.append({
            "account": name, "tier": t, "fit": c.get("fit"), "why_it_fits": c.get("fit_why"),
            "fresh_signals": fresh[:5],
            "people": [{**{k: p.get(k) for k in ("id", "name", "role", "kind", "warm", "evidence", "url", "stage", "confidence")},
                        "distance": dist[p["id"]]} for p in ppl],
            "last_contacted": str(touched) if touched else None,
            "cooling_down": bool(touched and (today() - touched).days < COOLDOWN_DAYS),
        })
    out.sort(key=lambda a: (a["tier"], -max(e["strength"] for e in a["fresh_signals"]), -(a["fit"] or 0)))
    return {"week": week_id(), "accounts": out}


def validate(m: dict, companies, people, jobs, moves) -> list[str]:
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
    sig = m.get("signal") or {}
    d = parse_date(sig.get("date"))
    if not sig.get("url"):
        errs.append("signal has no link")
    if not d:
        errs.append("signal has no date")
    elif (today() - d).days > FRESH_DAYS:
        errs.append(f"signal is {(today() - d).days} days old (limit {FRESH_DAYS})")
    known = {e["url"] for e in events(acct, c, jobs)}
    if sig.get("url") and sig["url"] not in known:
        errs.append("signal isn't one of this account's recorded events (add it to the account first)")
    touched = last_touch(acct, moves)
    if touched and (today() - touched).days < COOLDOWN_DAYS and not (d and d > touched):
        errs.append(f"contacted {(today() - touched).days} days ago and nothing new since")
    for k in ("why_now", "angle", "draft"):
        if not (m.get(k) or "").strip():
            errs.append(f"missing {k}")
    return errs


def plan(path) -> None:
    proposed = json.loads(open(path).read())
    companies, people, jobs = read(COMPANIES, {}), read(PEOPLE, {}), read(JOBS, {})
    wk = week_id()
    with locked(MOVES) as moves:
        open_this_week = [m for m in moves.values() if m.get("week") == wk and m.get("status") == "todo"]
        room = MAX_MOVES - len(open_this_week)
        used_accounts = {m["account"] for m in open_this_week}
        kept = 0
        for m in proposed:
            errs = validate(m, companies, people, jobs, moves)
            if m.get("account") in used_accounts:
                errs.append("already a move for this account this week")
            if errs:
                print(f"  ✗ {m.get('account')} / {m.get('person_id')}: " + "; ".join(errs))
                continue
            if kept >= room:
                print(f"  – {m['account']}: valid, but this week already has {MAX_MOVES} moves")
                continue
            sig = m["signal"]
            mid = f"{wk}:{m['person_id']}"
            moves[mid] = {
                "id": mid, "week": wk, "account": m["account"], "person_id": m["person_id"],
                "signal": {k: sig.get(k) for k in ("type", "summary", "url", "date")},
                "why_now": m["why_now"].strip(), "angle": m["angle"].strip(), "draft": m["draft"].strip(),
                "channel": m.get("channel") or people[m["person_id"]].get("platform") or "linkedin",
                "give_first": (m.get("give_first") or "").strip(),
                "status": "todo", "outcome": None, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            used_accounts.add(m["account"])
            kept += 1
            print(f"  ✓ {m['account']} → {people[m['person_id']]['name']}: {sig.get('summary', '')[:70]}")
    print(f"{kept} moves added for {wk}")


def funnel(days=90) -> dict:
    since = today() - timedelta(days=days)
    moves = [m for m in read(MOVES, {}).values() if (parse_date(m.get("created")) or today()) >= since]
    jobs, companies = read(JOBS, {}), read(COMPANIES, {})
    fresh = sum(1 for n, c in companies.items() if tier(c) in ("A", "B") for e in events(n, c, jobs) if e["fresh"])
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


if __name__ == "__main__":
    main()
