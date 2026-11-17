"""How long the cockpit's data takes to build and send, on a made-up install the size of a real one.

    python3 tests/bench_state.py            # build the install in a temporary folder, time it, remove it
    python3 tests/bench_state.py --profile  # also show where state() spends its time

Not a test: nothing here passes or fails. It copies this checkout's code into a temporary folder, fills data/ with
made-up roles, companies, people, events, moves and connections (every name invented), starts the cockpit there and
times /api/state the way a browser asks for it: the first load, then the 60-second refresh.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROLES, SKIPPED, COMPANIES, PEOPLE, EVENTS, MOVES, CONNECTIONS, SIGNALS, USAGE = 1300, 1100, 360, 130, 400, 60, 1500, 800, 3000
WORDS = ("platform product roadmap customers discovery data teams ship growth payments models agents enterprise pricing "
         "research design engineering strategy metrics launch workflow partners markets insight delivery quality").split()
A = "Amber Birch Cobalt Dune Ember Fjord Garnet Harbor Indigo Juniper Kestrel Lumen Marble Nimbus Onyx Pebble Quartz Russet Sable Tundra".split()
B = "field works labs forge light grid loop stack path wave yard mill gate line cast bridge".split()
TITLES = ["Senior Product Manager", "Product Manager, Platform", "Staff Product Manager", "Group Product Manager", "Product Owner",
          "Principal Product Manager, Growth", "Forward Deployed Product Manager", "Product Lead", "Sr. Product Manager (Payments)"]
PLACES = ["London", "London, UK (Hybrid)", "Remote, UK", "Berlin", "New York, NY", "Remote (EMEA)", "Dublin"]
FIRST = "Alex Blake Casey Devon Ellis Finley Gray Harper Indra Jules Kit Lane Morgan Noor Oakley Perry Quinn Reese Sage Tate".split()
LAST = "Ashdown Brightwater Coldharbour Dunmore Elmhurst Fairweather Greywell Hollins Ironside Larkfield".split()


def text(rng, n):
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


def build(app: Path) -> None:
    rng = random.Random(7)
    today = date.today()
    day = lambda back: str(today - timedelta(days=back))
    at = lambda back: (datetime.now(timezone.utc) - timedelta(days=back)).isoformat(timespec="seconds")
    names = [f"{a}{b}" for a in A for b in B][:COMPANIES]
    data = app / "data"
    data.mkdir(exist_ok=True)

    companies = {}
    for i, n in enumerate(names):
        companies[n] = {"status": rng.choice(["watched", "watched", "maybe", "candidate"]), "fit": rng.choice([None, 5, 6, 7, 8, 9]),
                        "fit_why": text(rng, 30), "product": text(rng, 25), "london": text(rng, 8), "stage": "Series B",
                        "board": f"greenhouse:{n.lower()}" if i % 3 else None, "added": day(rng.randint(1, 200)), "assessed": day(5),
                        "investors": [f"{rng.choice(A)} Capital", f"{rng.choice(A)} Ventures"], "headcount": "200-500",
                        "customers": text(rng, 12), "careers_url": f"https://{n.lower()}.example/careers",
                        "product_leaders": [f"{rng.choice(FIRST)} {rng.choice(LAST)}"], "favourite": i % 40 == 0}

    jobs = {}
    for i in range(ROLES):
        stage = "skipped" if i < SKIPPED else rng.choice(["new"] * 6 + ["shortlisted", "preparing", "applied", "applied", "interviewing", "closed"])
        company = rng.choice(names) + rng.choice(["", "", " Ltd", " Inc."])
        jid = f"gh:{company.split()[0].lower()}:{100000 + i}"
        score = rng.randint(1, 4) if stage == "skipped" and i % 5 else rng.randint(5, 10)
        jobs[jid] = {"id": jid, "company": company, "title": rng.choice(TITLES), "location": rng.choice(PLACES),
                     "url": f"https://boards.example/{jid.replace(':', '/')}", "description": text(rng, 700), "posted": day(rng.randint(1, 90)),
                     "stage": stage, "history": [{"stage": stage, "at": at(rng.randint(0, 60))}], "score": score, "why": text(rng, 45),
                     "flags": [text(rng, 6), text(rng, 6)], "source": rng.choice(["board", "board", "board", "adzuna", "email"]),
                     "text_kind": "posting", "first_seen": at(rng.randint(20, 90)), "last_seen": at(1), "missed": 0,
                     "board": {"ats": "greenhouse", "slug": company.split()[0].lower()} if i % 4 == 0 else None,
                     "link_checked": day(3), "also": {f"adzuna:{i}": {"url": f"https://jobs.example/{i}", "source": "adzuna", "seen": day(2)}} if i % 6 == 0 else {}}
        if stage == "skipped" and i % 7 == 0:
            jobs[jid]["skip_reason"] = rng.choice(["role", "level", "domain", "location", "pay"])

    people = {}
    while len(people) < PEOPLE:
        name, company = f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.choice(names)
        people[f"{name.lower().replace(' ', '-')}@{company.lower()}"] = {
            "name": name, "company": company, "role": "Director of Product", "platform": "linkedin", "url": f"https://linkedin.example/in/{len(people)}",
            "kind": rng.choice(["product_leader", "hiring_manager", "peer_pm", "recruiter"]), "evidence": text(rng, 20), "evidence_date": day(30),
            "confidence": "high", "past_companies": rng.sample(names, 3), "stage": rng.choice(["mapped"] * 4 + ["contacted", "replied"]),
            "added": day(40), "warm": len(people) % 9 == 0}

    events = {}
    for i in range(EVENTS):
        account, kind = rng.choice(names), rng.choice(["funding", "leader_hire", "post_hiring", "launch", "mention", "role_posted"])
        events[f"{account.lower()}:{kind}:{day(i % 120)}-{i}"] = {"account": account, "type": kind, "summary": text(rng, 25),
                                                                  "url": f"https://news.example/{i}", "date": day(i % 120), "source": "agent", "added": day(i % 120)}
    pids, eids = list(people), list(events)
    moves = {f"m{i}": {"id": f"m{i}", "person_id": rng.choice(pids), "account": rng.choice(names), "event_id": rng.choice(eids), "angle": text(rng, 30),
                       "draft": text(rng, 90), "status": rng.choice(["todo", "done", "done", "skipped"]), "created": at(i), "done_at": at(i),
                       "outcome": rng.choice([None, "replied", "conversation"]), "week": "2026-W01"} for i in range(MOVES)}
    signals = {f"s{i}": {"title": text(rng, 12), "url": f"https://feeds.example/{i}", "status": rng.choice(["new", "seen", "promoted"]),
                         "summary": text(rng, 40), "found": day(i % 60)} for i in range(SIGNALS)}
    network = {"imported": day(10), "connections": [
        {"name": f"{rng.choice(FIRST)} {rng.choice(LAST)}", "url": f"https://linkedin.example/in/c{i}", "position": "Product Manager",
         "company": rng.choice(names) + rng.choice(["", " Ltd"]) if i % 4 == 0 else f"{rng.choice(A)} {rng.choice(B).title()} Partners",
         "connected_on": "01 Jan 2020"} for i in range(CONNECTIONS)]}

    for name, value in (("companies", companies), ("jobs", jobs), ("people", people), ("events", events), ("moves", moves),
                        ("signals", signals), ("network", network)):
        (data / f"{name}.json").write_text(json.dumps(value, indent=2))
    (data / "usage.jsonl").write_text("".join(json.dumps({"kind": "ai", "task": rng.choice(["triage", "discover", "moves"]), "cost_usd": 0.02,
                                                           "input_tokens": 4000, "output_tokens": 500, "at": at(i % 45)[:19]}) + "\n" for i in range(USAGE)))
    (data / "heartbeat.log").write_text("\n".join(at(i) for i in range(60, 0, -1)))
    (app / "watchlist.csv").write_text("company,ats,slug,sector\n" + "".join(f"{n},greenhouse,{n.lower()},software\n" for n in names[::2]))
    (app / "profile").mkdir(exist_ok=True)
    (app / "profile" / "profile.md").write_text("# Candidate profile\n\n## What I want\n- Made-up target roles\n")
    (app / "settings.json").write_text(json.dumps({"where_groups": "London: london\nRemote: remote, anywhere\nElsewhere: *",
                                                    "role_kinds": "Forward deployed: forward deployed\nProduct: *",
                                                    "my_employers": "Amberfield\nCobaltworks"}))


TIMER = r"""
import gzip, json, sys, threading, time, urllib.request
from http.server import ThreadingHTTPServer
from radar import server

def ms(fn, n=5):
    best = None
    for _ in range(n):
        t = time.perf_counter(); out = fn(); took = (time.perf_counter() - t) * 1000
        best = took if best is None else min(best, took)
    return best, out

if "--profile" in sys.argv:
    import cProfile, pstats
    p = cProfile.Profile(); p.enable(); server.state(); p.disable()
    pstats.Stats(p).sort_stats("cumulative").print_stats(28)

httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{httpd.server_address[1]}/api/state"

def get(headers):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)

t = time.perf_counter(); status, body, headers = get({"Accept-Encoding": "gzip"}); first = (time.perf_counter() - t) * 1000
plain = gzip.decompress(body) if headers.get("Content-Encoding") == "gzip" else body
tag = headers.get("ETag")
again, (status2, body2, _) = ms(lambda: get({"Accept-Encoding": "gzip", **({"If-None-Match": tag} if tag else {})}))
s = json.loads(plain)
print(json.dumps({"first_ms": round(first), "refresh_ms": round(again, 1), "refresh_status": status2, "sent_bytes": len(body),
                  "refresh_bytes": len(body2), "json_bytes": len(plain), "compressed": headers.get("Content-Encoding") == "gzip",
                  "roles": len(s["jobs"]), "accounts": len(s["accounts"]), "people": len(s["people"]), "events": len(s["events"])}))
"""


def main():
    base = Path(tempfile.mkdtemp(prefix="gus-bench-"))
    try:
        app = base / "app"
        files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True)
        for name in filter(None, files.stdout.split("\n")):
            if (ROOT / name).is_file():
                (app / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, app / name)
        build(app)
        out = subprocess.run([sys.executable, "-c", TIMER, *sys.argv[1:]], cwd=app, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(base)})
        if out.returncode:
            sys.exit(out.stderr)
        *profile, result = out.stdout.strip().split("\n")
        print("\n".join(profile))
        r = json.loads(result)
        print(f"{r['roles']} roles, {r['accounts']} accounts, {r['people']} people, {r['events']} events")
        print(f"first load:  {r['first_ms']} ms, {r['sent_bytes'] / 1e6:.2f} MB sent ({r['json_bytes'] / 1e6:.2f} MB of JSON, {'gzip' if r['compressed'] else 'not compressed'})")
        print(f"refresh:     {r['refresh_ms']} ms, {r['refresh_bytes']} bytes sent (HTTP {r['refresh_status']})")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
