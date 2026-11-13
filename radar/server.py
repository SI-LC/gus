"""The Gus cockpit: a tiny local web app over the data files.

    python3 -m radar.server                 # http://127.0.0.1:8484 (this machine only)
    python3 -m radar.server --host 0.0.0.0  # reachable on your network, e.g. on a Pi

If a cockpit token is set (Settings), every request needs ?t=<token> once (it's then kept
in a cookie). Use that whenever the cockpit is reachable from other devices.
"""
from __future__ import annotations

import hmac
import json
import re
import socket
import sys
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from radar.companies import veto
from radar.jobs import role_key, same_company
from radar.matching import role_type, where
from radar.paths import COMPANIES, DATA, JOBS, PROFILE, ROOT, SIGNALS
from radar.schema import JOB_STAGES, OUTCOMES, PERSON_STAGES
from radar import firstrun, links, service, settings, sources, usage
from radar.settings import secret
from radar.store import locked, read

CLI_HOSTS: set = set()
PAGE = ROOT / "radar" / "cockpit.html"
MAX_BODY = 5_000_000
STATIC = {"/icon.svg": ("icon.svg", "image/svg+xml"), "/icon-32.png": ("icon-32.png", "image/png"), "/icon-180.png": ("icon-180.png", "image/png"),
          "/icon-512.png": ("icon-512.png", "image/png"), "/apple-touch-icon.png": ("icon-180.png", "image/png"),
          "/favicon.ico": ("icon-32.png", "image/png"), "/cockpit.css": ("cockpit.css", "text/css; charset=utf-8")}
MANIFEST = json.dumps({"name": "Gus", "short_name": "Gus", "start_url": "/", "display": "standalone",
                       "background_color": "#08090a", "theme_color": "#08090a",
                       "icons": [{"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
                                 {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}).encode()
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self'; manifest-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
RUN_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")



def last_agent_run():
    """The newest time in the run file names. Names start with different words, so sorting them gives the wrong one."""
    times = [m.group() for p in (DATA / "runs").glob("*.md") if (m := RUN_TIME.search(p.name))]
    if not times:
        return None
    day, clock = max(times).split("T")
    return f"{day}T{clock.replace('-', ':')}"


def state():
    watched = links.watched()
    cfg = settings.load()
    places, kinds = settings.groups(cfg.get("where_groups")), settings.groups(cfg.get("role_kinds"))
    jobs = [{**{k: v for k, v in j.items() if k != "description"}, "where": where(j.get("location"), places), "role_type": role_type(j.get("title"), kinds),
             "co": role_key(j)[0], "tk": role_key(j)[1],  # the same employer, and the same title, however each ad spells them
             "can_watch": bool(j.get("board")) and same_company(j.get("company")) not in watched} for j in read(JOBS, {}).values()]
    signals = read(SIGNALS, {})
    beats = (DATA / "heartbeat.log").read_text().split() if (DATA / "heartbeat.log").exists() else []
    from radar import accounts as A, events as E
    from radar.network import distances, summary as net_summary
    companies, people, moves, raw_jobs = read(COMPANIES, {}), read(A.PEOPLE, {}), read(A.MOVES, {}), read(JOBS, {})
    stored_events = read(A.EVENTS, {})
    dist = distances(people)
    accts = []
    for name, c in companies.items():
        t = A.tier(c)
        if t == "-":
            continue
        ev = A.events(name, stored=stored_events)
        touched = A.last_touch(name, moves)
        accts.append({"name": name, "tier": t, "tier_locked": bool(c.get("tier_locked")), "fit": c.get("fit"),
                      "why": c.get("fit_why"), "product": c.get("product"), "london": c.get("london"),
                      "stage": c.get("stage"), "board": c.get("board"), "board_broken": c.get("board_broken"),
                      "added": c.get("added"), "previous_fit": c.get("previous_fit"), "favourite": bool(c.get("favourite")),
                      "events": ev[:6], "fresh": sum(e["fresh"] for e in ev),
                      "careers_url_broken": bool(c.get("careers_url_broken")),
                      "last_contacted": str(touched) if touched else None})
    guesses = [{"name": n, **c["board_guess"]} for n, c in companies.items() if c.get("board_guess") and not c.get("vetoed")]
    shown = sorted(moves.values(), key=lambda m: m.get("created", ""), reverse=True)[:60]
    for m in shown:  # the event a move is built on, as it is now
        ev = stored_events.get(m.get("event_id") or "")
        m["signal"] = {k: ev.get(k) for k in ("type", "summary", "url", "date")} if ev else m.get("signal") or {}
    return {
        "setup": firstrun.setup_state(),
        "places": list(dict.fromkeys(p[0] for p in places)),
        "kinds": list(dict.fromkeys(k[0] for k in kinds)),
        "kind_default": kinds[-1][0] if kinds else "",
        "jobs": jobs,
        "accounts": accts,
        "events": sorted((E.decorate(dict(e, id=eid)) for eid, e in stored_events.items()),
                         key=lambda e: (not e["fresh"], -e["strength"], e["age_days"] if e["age_days"] is not None else 999)),
        "board_guesses": guesses,
        "people": {pid: dict(p, id=pid, distance=dist[pid]) for pid, p in people.items()},
        "moves": shown,
        "week": A.week_id(),
        "funnel": A.funnel(),
        "network": net_summary(),
        "running": list(service.status()["running"]),
        "follow_ups": follow_ups(raw_jobs, people, moves),
        "progress": service.status()["progress"],
        "last_runs": service.status()["last"],
        "ai": ai_state(),
        "meetings": sorted((dict(m, id=k) for k, m in read(DATA / "meetings.json", {}).items()
                            if not m.get("cancelled") and str(m.get("start", "")) >= now()[:10]), key=lambda m: str(m.get("start")))[:20],
        "usage": {**usage.summary(), "daily_limit": float(settings.load().get("ai_daily_budget_usd") or 0)},
        "labels": {"events": E.LABEL, "event_types": E.TYPES, "outcomes": OUTCOMES, "person_stages": PERSON_STAGES,
                   "role_stages": JOB_STAGES},
        "health": {
            "last_scan": next((b for b in reversed(beats) if "T" in b), None),
            "signals_waiting": sum(1 for l in signals.values() if l.get("status") == "new"),
            "board_errors": read(DATA / "board_errors.json", {}),
            "feed_errors": read(DATA / "feed_errors.json", {}),
            "x": read(DATA / "x_state.json", {}),
            "sources": sources.status(),
            "last_agent_run": last_agent_run(),
            "boards": len(watched),
            "claude": mcp_status(),
        },
    }


def settings_payload():
    from radar.feeds import SUGGESTED, all_feeds
    return {
        "schema": [dict(zip(("key", "label", "type", "group", "help"), r)) for r in settings.SCHEMA],
        "values": settings.load(),
        "providers": settings.PROVIDERS,
        "models": settings.CLAUDE_MODELS,
        "secret_keys": {k: {"label": l, "help": h} for k, (l, h) in settings.SECRET_KEYS.items()},
        "secrets": settings.secrets_masked(),
        "feeds": [{k: f[k] for k in ("name", "url", "kind", "enabled")} for f in all_feeds()],
        "suggested": [{"name": n, "url": u, "kind": k} for n, u, k in SUGGESTED],
        "run": service.status(),
        "sources": sources.status(),
        "claude": mcp_status(),
    }


def mcp_status():
    from radar import mcp
    return mcp.status()


def allowed_hosts() -> set:
    """Names this cockpit answers to: this machine, its hostname, and your Cockpit address setting."""
    names = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower(), socket.gethostname().split(".")[0].lower()}
    names |= {f"{n}.local" for n in list(names) if "." not in n and n != "localhost"}
    url = settings.load().get("cockpit_url") or ""
    if url:
        names.add((urlparse(url if "//" in url else "//" + url).hostname or "").lower())
    return names | set(CLI_HOSTS)


KIND_REASON = {"hiring_manager": "Likely hiring manager for this role", "product_leader": "Leads product in this area",
               "recruiter": "Recruits for this company", "peer_pm": "A PM here who can tell you about the team and may introduce you",
               "warm": "You already know them", "announcer": "Posted about hiring here"}
KIND_ORDER = list(KIND_REASON)
FOLLOW_UP_DAYS = 21
MAX_PER_ROLE = 3


def follow_ups(jobs: dict, people: dict, moves: dict, today=None) -> list:
    """Applied roles, with the people worth contacting about each and why. No AI."""
    today = today or datetime.now(timezone.utc)
    busy = {m.get("person_id") for m in moves.values() if m.get("status") in ("todo", "done")}
    out = []
    for j in jobs.values():
        if j.get("stage") != "applied":
            continue
        applied = next((h["at"] for h in reversed(j.get("history") or []) if h.get("stage") == "applied"), j.get("first_seen"))
        try:
            age = (today - datetime.fromisoformat(applied)).days
        except (TypeError, ValueError):
            age = None
        if age is not None and age > FOLLOW_UP_DAYS:
            continue
        done = set(j.get("contacts") or {})
        who = [dict(p, id=pid) for pid, p in people.items() if p.get("company") == j.get("company")
               and p.get("stage", "mapped") == "mapped" and pid not in done and pid not in busy]
        who.sort(key=lambda p: (not p.get("warm"), KIND_ORDER.index(p["kind"]) if p.get("kind") in KIND_ORDER else 99))
        contacts = [{"id": p["id"], "name": p.get("name"), "role": p.get("role"), "url": p.get("url"), "platform": p.get("platform"),
                     "reason": KIND_REASON.get(p.get("kind"), "Works at this company"), "evidence": p.get("evidence"),
                     "confidence": p.get("confidence")} for p in who[:MAX_PER_ROLE]]
        anyone = any(p.get("company") == j.get("company") for p in people.values())
        if contacts or not (done or anyone):  # nothing left to do once someone's been contacted
            out.append({"id": j["id"], "company": j.get("company"), "title": j.get("title"), "days": age, "contacts": contacts})
    return sorted(out, key=lambda r: (r["days"] is None, r["days"] or 0))


def ai_state():
    """Whether AI runs can happen right now, so the cockpit can say so instead of looking broken."""
    limit, spent = float(settings.load().get("ai_daily_budget_usd") or 0), usage.today_cost()
    from radar import ai
    expired = ai.login_expired() if settings.load().get("ai_provider", "claude_subscription") == "claude_subscription" else None
    reason = "off" if not limit else "signed_out" if expired else "limit" if spent >= limit else None
    return {"limit": limit, "spent": round(spent, 2), "paused": reason, "signed_out_since": (expired or {}).get("since"),
            "on_plan": settings.load().get("ai_provider", "claude_subscription") == "claude_subscription"}


def role_contact(body):
    """You messaged a person about a role: record it on the role, their move and their stage."""
    from radar import accounts as A
    job_id, pid, sent = body.get("id"), body.get("person_id"), bool(body.get("sent", True))
    if pid not in read(A.PEOPLE, {}):
        return {"error": "No such person"}
    with locked(JOBS) as store:
        j = store.get(job_id)
        if not j:
            return {"error": "No such role"}
        contacts = j.setdefault("contacts", {})
        if sent:
            contacts.setdefault(pid, now())
        else:
            contacts.pop(pid, None)
    with locked(A.MOVES) as moves:
        for m in moves.values():
            if m.get("person_id") != pid:
                continue
            if sent and m.get("status") == "todo":
                m.update(status="done", done_at=now())
            elif not sent and m.get("status") == "done" and not m.get("outcome"):
                m.update(status="todo", done_at=None)
    with locked(A.PEOPLE) as people:
        p = people[pid]
        if sent and p.get("stage", "mapped") == "mapped":
            p["stage"] = "contacted"
            p.setdefault("history", []).append({"stage": "contacted", "at": now()})
        elif not sent and p.get("stage") == "contacted":
            p["stage"] = "mapped"
            p.setdefault("history", []).append({"stage": "mapped", "at": now()})
    return {"ok": True}


def update_event(body):
    """Record something that happened at an account by hand, correct it, or dismiss it. Nothing is deleted: a move may cite it."""
    from radar import events as E
    if not body.get("id"):
        if body.get("account") not in read(COMPANIES, {}):
            return {"error": "No such account"}
        try:
            eid = E.add(str(body["account"]), str(body.get("type") or ""), str(body.get("summary") or ""),
                        str(body.get("url") or ""), str(body.get("date") or ""))
        except ValueError as e:
            return {"error": str(e)}
        return {"ok": True, "id": eid}
    with locked(E.EVENTS) as events:
        e = events.get(body.get("id"))
        if not e:
            return {"error": "No such event"}
        if body.get("type") in E.TYPES:
            e["type"] = body["type"]
        if "summary" in body:
            e["summary"] = str(body["summary"] or "").strip()[:300]
        if body.get("date"):
            d = E.parse_date(body["date"])
            if not d:
                return {"error": "needs a real date (YYYY-MM-DD)"}
            e["date"] = str(d)
        if "dismissed" in body:  # a dismissed event stops counting, like a role whose ad closed
            if e.get("type") == "role_posted" and e.get("job_id"):
                return {"error": "This follows the role: skip the role instead"}
            if body["dismissed"]:
                e["closed"] = now()[:10]
            else:
                e.pop("closed", None)
    return {"ok": True, "id": body["id"]}


def update_move(body):
    """Mark a move done or skipped, record what came of it, or edit its draft. The person's stage follows the move."""
    from radar import accounts as A
    with locked(A.MOVES) as moves:
        m = moves.get(body.get("id"))
        if not m:
            return {"error": "No such move"}
        if body.get("status") in ("todo", "done", "skipped"):
            m["status"] = body["status"]
            m["done_at"] = now() if body["status"] == "done" else None
        if body.get("outcome") in OUTCOMES + [None]:
            m["outcome"], m["outcome_at"] = body.get("outcome"), now()
        if "draft" in body:
            m["draft"] = str(body["draft"])[:3000]
        pid, status, outcome = m["person_id"], m["status"], m.get("outcome")
    with locked(A.PEOPLE) as people:
        p = people.get(pid)
        if p:
            order = PERSON_STAGES
            target = {"replied": "replied", "conversation": "conversation", "interview": "conversation",
                      "referral": "referred"}.get(outcome) or ("contacted" if status == "done" else None)
            if target and order.index(target) > order.index(p.get("stage", "mapped")):
                p["stage"] = target
                p.setdefault("history", []).append({"stage": target, "at": now()})
    return {"ok": True}


def update_person(body):
    """Add a person by hand (a name and no id), or change the stage, note or LinkedIn degree of one Gus has."""
    from radar import accounts as A
    created = None
    if not body.get("id") and body.get("name"):
        try:
            body["id"], created = A.add_person(*(str(body.get(k) or "") for k in ("name", "company", "role", "url", "kind", "evidence")))
        except ValueError as e:
            return {"error": str(e)}
    with locked(A.PEOPLE) as people:
        p = people.get(body.get("id"))
        if not p:
            return {"error": "No such person"}
        if body.get("stage") in PERSON_STAGES:
            p["stage"] = body["stage"]
            p.setdefault("history", []).append({"stage": body["stage"], "at": now()})
        if "note" in body:
            p["note"] = str(body["note"])[:2000]
        if "degree" in body:  # what LinkedIn shows on their profile
            d = body["degree"]
            if d in (1, 2, 3):
                p["degree"], p["via"] = d, str(body.get("via") or "")[:80].strip()
                p["degree_at"] = now()
            else:
                for k in ("degree", "via", "degree_at"):
                    p.pop(k, None)
    return {"ok": True, "id": body["id"]} | ({"created": created} if created is not None else {})


def update_account(body):
    """Set an account's tier yourself, or mark it as one you love."""
    with locked(COMPANIES) as companies:
        c = companies.get(body.get("name"))
        if not c:
            return {"error": "No such account"}
        if body.get("tier") in ("A", "B", "C"):
            c["tier"], c["tier_locked"] = body["tier"], True
        if "favourite" in body:
            c["favourite"] = bool(body["favourite"])
    return {"ok": True}


def add_company(body):
    """Add a company to watch from the cockpit: find its job board the way `radar.resolve --add` does, then make it an account.
    Without a public board it's still an account, with no board until the watchlist agent or you find one."""
    name = " ".join(str(body.get("name") or "").split())[:80]
    careers = str(body.get("careers") or "").strip() or None
    if not name:
        return {"error": "Needs a company name"}
    if careers and not re.match(r"https?://", careers):
        return {"error": "The careers address needs to start with http"}
    if name.lower() in {n.lower() for n in read(COMPANIES, {})} or same_company(name) in links.watched():
        return {"error": f"{name} is already on your list"}
    hit = watch_company(name, careers)
    return {"ok": True, "name": name, "board": f"{hit[0]}:{hit[1]}" if hit else None, "open_roles": hit[2] if hit else 0}


def watch_company(name, careers=None):
    """Make a company an account and watch its public job board, if it has one Gus can find. Returns (ats, slug, open roles) or None."""
    from radar import resolve
    hit = resolve.resolve(name, careers)  # network, outside the lock
    if hit:
        resolve.add_board(name, hit[0], hit[1])
    with locked(COMPANIES) as companies:
        c = companies.setdefault(name, {})
        c.setdefault("status", "watched")
        c.setdefault("added", now()[:10])
        c.setdefault("source", "manual")
        if careers:
            c["careers_url"] = careers
        if hit:
            c["board"] = f"{hit[0]}:{hit[1]}"
    return hit


def clean_folder(value):
    """(folder, problem). A web link or a path on your computer: it lives in data/ only, never in the repo."""
    folder = str(value or "").strip()
    if "\n" in folder or len(folder) > 2000:
        return None, "That doesn't look like a folder path or link"
    if "://" in folder and not folder.lower().startswith(("https://", "http://")):
        return None, "Use a web link starting with https://, or a folder path"
    return folder, None


MAX_DESCRIPTION = 60_000
START_STAGES = [s for s in JOB_STAGES if s not in ("closed", "skipped")]


def add_job(body):
    """Add a role by hand: one a recruiter brought you, say, with no public ad for a scan to find. It starts shortlisted
    unless you say otherwise, and belongs to its company's account. A company new to Gus is added the way "Add a company"
    adds one: its public job board is looked for there and then (surer with its `careers` address) and watched, so its
    other roles arrive with the next scan. The role is scored like any other, and is never skipped by its score or closed because a board stopped listing it."""
    from radar import jobs as J
    line = lambda k, n: " ".join(str(body.get(k) or "").split())[:n]
    company, title, location = line("company", 80), line("title", 200), line("location", 120)
    url, stage = str(body.get("url") or "").strip(), body.get("stage") or "shortlisted"
    careers = str(body.get("careers") or "").strip() or None
    if not company or not title:
        return {"error": "Needs a company and a title"}
    if url and not re.match(r"https?://\S+$", url):
        return {"error": "The link needs to start with http"}
    if careers and not re.match(r"https?://", careers):
        return {"error": "The careers address needs to start with http"}
    if stage not in START_STAGES:
        return {"error": f"The stage must be one of: {', '.join(START_STAGES)}"}
    folder, problem = clean_folder(body.get("folder"))
    if problem:
        return {"error": problem}
    same = lambda n: n.lower() == company.lower() or (same_company(n) and same_company(n) == same_company(company))
    company = next((n for n in read(COMPANIES, {}) if same(n)), company)  # the account's own spelling, so the role is found under it
    role = {"id": J.manual_id(company, title), "company": company, "title": title, "url": url, "location": location,
            "description": str(body.get("description") or "").strip()[:MAX_DESCRIPTION], "text_kind": "posting", "posted": None}
    with locked(JOBS) as store:
        if (have := J.find_match(store, role)) is not None:
            j = store[have]
            return {"error": f"Gus has this role already: {j.get('title')} at {j.get('company')}", "id": have}
        role.update(first_seen=now(), last_seen=now(), stage=stage, history=[{"stage": stage, "at": now()}], score=None, why=None, source="manual")
        if folder:
            role["folder"] = folder
        if body.get("note"):
            role["note"] = str(body["note"])[:2000]
        store[role["id"]] = role
    new_account = company not in read(COMPANIES, {})
    hit = watch_company(company, careers) if new_account else None  # the role is saved by now, whatever the lookup finds
    return {"ok": True, "id": role["id"], "new_account": new_account, "board": f"{hit[0]}:{hit[1]}" if hit else None,
            "open_roles": hit[2] if hit else 0, "job": {k: v for k, v in role.items() if k != "description"}}


DETAILS = {"title": 200, "location": 120}


def get_job(jid):
    """One role with everything on it, its description included: /api/state leaves that out, as it's most of the file."""
    j = read(JOBS, {}).get(jid)
    return {"ok": True, "job": j} if j else {"error": "No such role"}


def update_job(body):
    with locked(JOBS) as store:
        j = store.get(body.get("id"))
        if not j:
            return {"error": "No such role"}
        if any(k in body for k in (*DETAILS, "url", "description")):  # what the ad says: yours to correct only on a role you added
            if j.get("source") != "manual":
                return {"error": "Only a role you added by hand can be edited: this one follows its ad"}
            changes = {k: " ".join(str(body[k] or "").split())[:n] for k, n in DETAILS.items() if k in body}
            if "title" in changes and not changes["title"]:
                return {"error": "Needs a title"}
            if "url" in body:
                changes["url"] = str(body["url"] or "").strip()
                if changes["url"] and not re.match(r"https?://\S+$", changes["url"]):
                    return {"error": "The link needs to start with http"}
            if "description" in body:
                changes["description"] = str(body["description"] or "").strip()[:MAX_DESCRIPTION]
                if changes["description"] != (j.get("description") or ""):
                    changes.update(score=None, why=None, flags=[])  # scored on the old words: the next scan scores it again
                    j.pop("score_before_salary", None)
                    j.pop("pruned", None)
            j.update(changes)
        if body.get("stage") in JOB_STAGES and body["stage"] != j.get("stage"):
            j.setdefault("history", []).append({"stage": body["stage"], "at": now()})
            j["stage"] = body["stage"]
        if "note" in body:
            j["note"] = str(body["note"])[:2000]
        if "folder" in body:
            folder, problem = clean_folder(body["folder"])
            if problem:
                return {"error": problem}
            j["folder"] = folder
        return {"ok": True, "job": {k: v for k, v in j.items() if k != "description"}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def host_ok(self):
        """Refuse DNS rebinding: a website can point its own name at this machine, but the Host header gives it away."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in allowed_hosts()

    def authed(self):
        token = secret("cockpit_token")
        if not token:
            return True
        q = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        c = SimpleCookie(self.headers.get("Cookie", "")).get("radar")
        self.token_in_url = bool(q) and hmac.compare_digest(q.encode(), token.encode())
        return self.token_in_url or (c is not None and hmac.compare_digest(c.value.encode(), token.encode()))

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        if getattr(self, "token_in_url", False):  # only ever after the token has been verified
            self.send_header("Set-Cookie", f"radar={secret('cockpit_token')}; HttpOnly; SameSite=Strict; Max-Age=31536000; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, b"Unknown host. Add it under Settings, Notifications, Cockpit address.", "text/plain")
        path = urlparse(self.path).path
        if path in STATIC:  # a fixed list, and nothing private: browsers fetch these before you've signed in
            name, ctype = STATIC[path]
            return self.send(200, (ROOT / "radar" / "static" / name).read_bytes(), ctype)
        if path == "/manifest.webmanifest":
            return self.send(200, MANIFEST, "application/manifest+json")
        if path == "/mcp":
            return self.send(405, {"error": "Claude connects here with POST. See Settings, Claude."})
        if not self.authed():
            return self.send(401, b"Add ?t=<your cockpit token> to the URL.", "text/plain")
        if path == "/":
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send(200, state())
        if path == "/api/job":
            return self.send(200, get_job(parse_qs(urlparse(self.path).query).get("id", [""])[0]))
        if path == "/api/settings":
            return self.send(200, settings_payload())
        if path == "/api/profile":
            return self.send(200, {"text": PROFILE.read_text() if PROFILE.exists() else ""})
        if path == "/api/run":
            return self.send(200, service.status())
        if path == "/api/first-run":
            return self.send(200, firstrun.status())
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            return self.send(413, {"error": "Too large"})
        raw = self.rfile.read(n)  # before any refusal: closing on unread data resets the connection, and the caller never learns why
        if not self.host_ok():
            return self.send(403, {"error": "Unknown host"})
        path = urlparse(self.path).path
        who = None
        if path == "/mcp":  # Claude, not the cockpit: its own token, and the cockpit's token doesn't open it
            from radar import mcp
            ok, who = mcp.allowed(self.headers)
            if not ok:
                return self.send(401, {"error": who})
        elif not self.authed():
            return self.send(401, {"error": "Not signed in"})
        if self.headers.get("Origin") and urlparse(self.headers["Origin"]).netloc != self.headers.get("Host"):
            return self.send(403, {"error": "Cross-site request refused"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self.send(415, {"error": "Send JSON"})  # plain forms from other sites can't set this
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self.send(400, {"error": "Bad JSON"})
        if not isinstance(body, dict):
            return self.send(400, {"error": "Bad JSON"})
        if path == "/mcp":
            reply = mcp.handle(body, who)
            return self.send(202, b"") if reply is None else self.send(200, reply)
        if path == "/api/mcp-token":  # made here and shown once; Gus keeps only its hash
            from radar import mcp
            if body.get("action") == "remove":
                mcp.remove_token()
                return self.send(200, {"ok": True, "claude": mcp.status()})
            token = mcp.new_token(self.headers.get("Tailscale-User-Login") or "")
            return self.send(200, {"ok": True, "token": token, "claude": mcp.status()})
        if path == "/api/job":
            return self.send(200, update_job(body))
        if path == "/api/job/add":
            return self.send(200, add_job(body))
        if path == "/api/watch":
            job = read(JOBS, {}).get(body.get("id"))
            return self.send(200, {"ok": bool(job) and links.watch(job)})
        if path == "/api/role-contact":
            return self.send(200, role_contact(body))
        if path == "/api/company/veto":
            if body.get("name") not in read(COMPANIES, {}):
                return self.send(200, {"error": "No such account"})
            veto(str(body["name"]))
            return self.send(200, {"ok": True})
        if path == "/api/company/board":
            from radar.companies import accept_board, reject_board
            if body.get("name") not in read(COMPANIES, {}):
                return self.send(200, {"error": "No such account"})
            return self.send(200, {"ok": (accept_board if body.get("accept") else reject_board)(str(body["name"]))})
        try:
            if path == "/api/settings":
                settings.save(body.get("changes") or {})
                return self.send(200, settings_payload())
            if path == "/api/secret":
                settings.set_secret(str(body.get("key")), str(body.get("value") or ""))
                return self.send(200, {"ok": True, "secrets": settings.secrets_masked()})
            if path == "/api/profile":
                PROFILE.parent.mkdir(exist_ok=True)
                PROFILE.write_text(str(body.get("text", "")))
                return self.send(200, {"ok": True})
            if path == "/api/move":
                return self.send(200, update_move(body))
            if path == "/api/person":
                return self.send(200, update_person(body))
            if path == "/api/event":
                return self.send(200, update_event(body))
            if path == "/api/company/add":
                return self.send(200, add_company(body))
            if path == "/api/account":
                return self.send(200, update_account(body))
            if path == "/api/network/import":
                from radar import network
                n = network.import_text(str(body.get("text", "")))
                hits = network.match()
                return self.send(200, {"ok": True, "connections": n, "matches": hits, "summary": network.summary()})
            if path == "/api/feeds/add":
                from radar.feeds import add_feed
                title, url = add_feed(str(body.get("text", "")), str(body.get("kind") or "newsletter"))
                return self.send(200, {"ok": True, "title": title, "url": url, **settings_payload()})
            if path == "/api/feeds/remove":
                from radar.feeds import remove_feed
                remove_feed(str(body.get("name", "")))
                return self.send(200, settings_payload())
            if path == "/api/first-run":
                return self.send(200, firstrun.update(body))
            if path == "/api/first-run/connect":
                return self.send(200, firstrun.connect(str(body.get("key") or "")))
            if path == "/api/ai-login":
                from radar import ai
                if settings.load().get("ai_provider", "claude_subscription") != "claude_subscription" and firstrun.set_up():
                    return self.send(200, {"error": "This install doesn't use a Claude login."})
                return self.send(200, ai.login_finish(str(body["code"])) if "code" in body else ai.login_start())
            if path == "/api/first-run/skip":
                return self.send(200, firstrun.skip())
            if path == "/api/first-run/hide":
                return self.send(200, firstrun.hide_get_started())
            if path == "/api/test-gmail":
                from radar import inbox
                return self.send(200, inbox.test())
            if path == "/api/test-ai":
                from radar import ai
                return self.send(200, ai.test())
            if path == "/api/run":
                job = body.get("job")
                if job not in ("run", "scan", "sources"):
                    return self.send(400, {"error": "Unknown job"})
                return self.send(200, {"ok": True, "started": service.start(job)})
        except ValueError as e:
            return self.send(200, {"error": str(e)})
        self.send(404, {"error": "Not found"})


def main():
    host = sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1"
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8484
    if host not in ("127.0.0.1", "localhost", "::1") and not secret("cockpit_token"):
        sys.exit("Refusing to listen on the network without a cockpit token. Set one in Settings, Keys, first.")
    if host not in ("0.0.0.0", "::"):
        CLI_HOSTS.add(host.lower())
    print(f"Gus cockpit on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
