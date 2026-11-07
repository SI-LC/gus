"""What each object in data/ looks like, in one place, and a check that the files still match.

    python3 -m radar.schema check          # every record in data/: missing fields, bad stages, wrong types
    python3 -m radar.schema fields jobs    # the fields of one object

The prose version is docs/objects.md. Records may carry extra fields (the research agents add what
they find); the check only complains about what's required, what must be one of a fixed set, and
types that would break the code. The code scan runs it, so a drift between a prompt and the code
shows up before a deploy, not on the Pi.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from radar.scan import DATA

STR, INT, BOOL, LIST, DICT, NUM = str, int, bool, list, dict, (int, float)

JOB_STAGES = ["new", "shortlisted", "preparing", "applied", "interviewing", "closed", "skipped"]
JOB_SOURCES = ["board", "hn", "email", "x", "web", "adzuna", "jsearch"]
COMPANY_STATUS = ["candidate", "watched", "maybe", "rejected"]
TIERS = ["A", "B", "C"]
EVENT_TYPES = ["post_hiring", "role_posted", "leader_hire", "funding", "london_office", "launch", "mention"]
PERSON_KINDS = ["product_leader", "hiring_manager", "peer_pm", "recruiter", "warm", "announcer"]
PERSON_STAGES = ["mapped", "contacted", "replied", "conversation", "referred", "closed"]
MOVE_STATUS = ["todo", "done", "skipped"]
OUTCOMES = ["no_reply", "replied", "conversation", "interview", "referral", "not_now"]
SIGNAL_STATUS = ["new", "processed", "ignored"]

# name -> (file, key pattern, required fields, {field: type}, {field: allowed values})
OBJECTS = {
    "jobs": ("jobs.json", r"^[a-z]+:", ["company", "title", "url", "stage"],
             {"company": STR, "title": STR, "url": STR, "location": STR, "description": STR, "score": INT, "why": STR,
              "flags": LIST, "history": LIST, "contacts": DICT, "note": STR, "folder": STR, "missed": INT},
             {"stage": JOB_STAGES, "source": JOB_SOURCES}),
    "companies": ("companies.json", r".", ["status"],
                  {"fit": NUM, "fit_why": STR, "events": LIST, "pending_signals": LIST, "investors": LIST,
                   "product_leaders": LIST, "tier_locked": BOOL, "favourite": BOOL, "vetoed": BOOL, "reassess": BOOL},
                  {"status": COMPANY_STATUS, "tier": TIERS}),
    "people": ("people.json", r"^[a-z0-9-]+@[a-z0-9-]+$", ["name", "company", "stage"],
               {"name": STR, "company": STR, "url": STR, "evidence": STR, "warm": BOOL, "past_companies": LIST,
                "history": LIST, "degree": INT, "note": STR},
               {"stage": PERSON_STAGES, "kind": PERSON_KINDS}),
    "moves": ("moves.json", r"^\d{4}-W\d{2}:", ["week", "account", "person_id", "signal", "why_now", "angle", "draft", "status"],
              {"signal": DICT, "draft": STR, "why_now": STR, "angle": STR},
              {"status": MOVE_STATUS, "outcome": OUTCOMES + [None]}),
    "signals": ("signals.json", r".", ["source", "url", "status"], {"url": STR, "text": STR, "title": STR},
                {"status": SIGNAL_STATUS}),
}
EVENT_FIELDS = ["type", "summary", "url", "date"]


def check_record(name: str, key: str, rec, companies: dict, people: dict) -> list[str]:
    _, pattern, required, types, allowed = OBJECTS[name]
    if not isinstance(rec, dict):
        return ["not an object"]
    errs = [f"key {key!r} doesn't look like a {name[:-1]} id"] if not re.search(pattern, key) else []
    errs += [f"missing {f}" for f in required if rec.get(f) in (None, "")]
    for f, t in types.items():
        if rec.get(f) is not None and not isinstance(rec[f], t) or (t is INT and isinstance(rec.get(f), bool)):
            errs.append(f"{f} should be {getattr(t, '__name__', 'a number')}")
    for f, vals in allowed.items():
        if rec.get(f) not in (None, "") and rec[f] not in vals:
            errs.append(f"{f}={rec[f]!r} isn't one of {[v for v in vals if v]}")
    if name == "jobs" and rec.get("score") is not None and not 1 <= rec["score"] <= 10:
        errs.append("score must be 1-10")
    if name == "companies":  # an old `why_it_fits` isn't flagged: companies sync renames it on the next scan
        for i, e in enumerate(rec.get("events") or []):
            if not isinstance(e, dict) or any(not e.get(f) for f in EVENT_FIELDS):
                errs.append(f"events[{i}] needs {', '.join(EVENT_FIELDS)}")
            elif e.get("type") not in EVENT_TYPES:
                errs.append(f"events[{i}] type {e['type']!r} isn't one of {EVENT_TYPES}")
    if name == "people" and rec.get("company") and companies and rec["company"] not in companies:
        errs.append(f"company {rec['company']!r} isn't in companies.json")
    if name == "moves":
        if rec.get("account") not in companies:
            errs.append(f"account {rec.get('account')!r} isn't in companies.json")
        if rec.get("person_id") not in people:
            errs.append(f"person {rec.get('person_id')!r} isn't in people.json")
        if isinstance(rec.get("signal"), dict) and not rec["signal"].get("url"):
            errs.append("signal has no url")
    return errs


def check(data: Path = DATA) -> dict[str, list[str]]:
    """{object: [problems]} for every data file that exists. Empty means all good."""
    loaded = {}
    for name, (file, *_) in OBJECTS.items():
        try:
            loaded[name] = json.loads((data / file).read_text())
        except FileNotFoundError:
            continue
        except ValueError as e:
            loaded[name] = e
    out = {}
    companies, people = loaded.get("companies"), loaded.get("people")
    companies = companies if isinstance(companies, dict) else {}
    people = people if isinstance(people, dict) else {}
    for name, recs in loaded.items():
        if isinstance(recs, Exception):
            out[name] = [f"not valid JSON: {recs}"]
        elif not isinstance(recs, dict):
            out[name] = ["should be an object keyed by id"]
        else:
            errs = [f"{k}: {e}" for k, r in recs.items() for e in check_record(name, k, r, companies, people)]
            if errs:
                out[name] = errs
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "fields":
        file, _, required, types, allowed = OBJECTS[sys.argv[2]]
        print(f"data/{file}\n  required: {', '.join(required)}")
        for f, vals in allowed.items():
            print(f"  {f}: {' | '.join(str(v) for v in vals if v)}")
        typed = [f"{f} ({getattr(t, '__name__', 'number')})" for f, t in types.items()]
        print(f"  typed: {', '.join(typed)}")
        return
    problems = check()
    for name, errs in problems.items():
        print(f"{name}: {len(errs)} problem{'s' if len(errs) != 1 else ''}")
        for e in errs[:40]:
            print(f"  {e}")
    if problems:
        sys.exit(1)
    print("data files match docs/objects.md")


if __name__ == "__main__":
    main()
