"""The PM Radar cockpit: a tiny local web app over the data files.

    python3 -m radar.server                 # http://127.0.0.1:8484 (this machine only)
    python3 -m radar.server --host 0.0.0.0  # reachable on your network, e.g. on a Pi

If a cockpit token is set (Settings), every request needs ?t=<token> once (it's then kept
in a cookie). Use that whenever the cockpit is reachable from other devices.
"""
from __future__ import annotations

import hmac
import json
import socket
import sys
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from radar.companies import COMPANIES, veto
from radar.scan import DATA, JOBS, ROOT, role_type, where
from radar import service, settings, usage
from radar.settings import secret
from radar.store import locked

CLI_HOSTS: set = set()
PAGE = ROOT / "radar" / "cockpit.html"
PROFILE = ROOT / "profile" / "profile.md"
MAX_BODY = 5_000_000
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
STAGES = {"new", "shortlisted", "preparing", "applied", "interviewing", "closed", "skipped"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def state():
    jobs = [{**{k: v for k, v in j.items() if k != "description"}, "where": where(j.get("location")),
             "role_type": role_type(j.get("title"))} for j in read(JOBS, {}).values()]
    signals = read(DATA / "signals.json", {})
    beats = (DATA / "heartbeat.log").read_text().split() if (DATA / "heartbeat.log").exists() else []
    runs = sorted(p.name for p in (DATA / "runs").glob("*.md")) if (DATA / "runs").exists() else []
    from radar import accounts as A
    from radar.network import distances, summary as net_summary
    companies, people, moves, raw_jobs = read(COMPANIES, {}), read(A.PEOPLE, {}), read(A.MOVES, {}), read(JOBS, {})
    dist = distances(people)
    accts = []
    for name, c in companies.items():
        t = A.tier(c)
        if t == "-":
            continue
        ev = A.events(name, c, raw_jobs)
        touched = A.last_touch(name, moves)
        accts.append({"name": name, "tier": t, "tier_locked": bool(c.get("tier_locked")), "fit": c.get("fit"),
                      "why": c.get("fit_why") or c.get("why_it_fits"), "product": c.get("product"), "london": c.get("london"),
                      "stage": c.get("stage"), "board": c.get("board"), "board_broken": c.get("board_broken"),
                      "added": c.get("added"), "previous_fit": c.get("previous_fit"),
                      "events": ev[:6], "fresh": sum(e["fresh"] for e in ev),
                      "last_contacted": str(touched) if touched else None})
    shown = sorted(moves.values(), key=lambda m: m.get("created", ""), reverse=True)[:60]
    for m in shown:  # always show a signal's current, corrected date, not the one stored at planning time
        live = next((e for e in A.events(m["account"], companies.get(m["account"], {}), raw_jobs)
                     if e["url"] == m["signal"].get("url")), None)
        if live:
            m["signal"] = {**m["signal"], "date": live["date"]}
    return {
        "jobs": jobs,
        "companies": companies,
        "accounts": accts,
        "people": {pid: dict(p, id=pid, distance=dist[pid]) for pid, p in people.items()},
        "moves": shown,
        "week": A.week_id(),
        "funnel": A.funnel(),
        "network": net_summary(),
        "running": list(service.status()["running"]),
        "meetings": sorted((dict(m, id=k) for k, m in read(DATA / "meetings.json", {}).items()
                            if not m.get("cancelled") and str(m.get("start", "")) >= now()[:10]), key=lambda m: str(m.get("start")))[:20],
        "usage": {**usage.summary(), "daily_limit": float(settings.load().get("ai_daily_budget_usd") or 0)},
        "labels": {"events": A.EVENT_LABEL, "outcomes": A.OUTCOMES, "person_stages": A.PERSON_STAGES},
        "health": {
            "last_scan": next((b for b in reversed(beats) if "T" in b), None),
            "signals_waiting": sum(1 for l in signals.values() if l.get("status") == "new"),
            "board_errors": read(DATA / "board_errors.json", {}),
            "x": read(DATA / "x_state.json", {}),
            "last_agent_run": runs[-1] if runs else None,
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
        "feeds": [{k: f[k] for k in ("name", "url", "kind", "enabled", "builtin")} for f in all_feeds()],
        "suggested": [{"name": n, "url": u, "kind": k} for n, u, k in SUGGESTED],
        "run": service.status(),
    }


def allowed_hosts() -> set:
    """Names this cockpit answers to: this machine, its hostname, and your Cockpit address setting."""
    names = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower(), socket.gethostname().split(".")[0].lower()}
    names |= {f"{n}.local" for n in list(names) if "." not in n and n != "localhost"}
    url = settings.load().get("cockpit_url") or ""
    if url:
        names.add((urlparse(url if "//" in url else "//" + url).hostname or "").lower())
    return names | set(CLI_HOSTS)


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


def update_job(body):
    with locked(JOBS) as store:
        j = store.get(body.get("id"))
        if not j:
            return {"error": "No such role"}
        if body.get("stage") in STAGES and body["stage"] != j.get("stage"):
            j.setdefault("history", []).append({"stage": body["stage"], "at": now()})
            j["stage"] = body["stage"]
        if "note" in body:
            j["note"] = str(body["note"])[:2000]
        if "folder" in body:  # a path on your own computer: lives in data/ only, never in the repo
            folder = str(body["folder"] or "").strip()
            if "\n" in folder or len(folder) > 500:
                return {"error": "That doesn't look like a folder path"}
            j["folder"] = folder
        if "person" in body:
            p = (j.get("people") or [])[int(body["person"])]
            p["sent_at"] = now() if body.get("sent", True) else None
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
        if not self.authed():
            return self.send(401, b"Add ?t=<your cockpit token> to the URL.", "text/plain")
        path = urlparse(self.path).path
        if path == "/":
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send(200, state())
        if path == "/api/settings":
            return self.send(200, settings_payload())
        if path == "/api/profile":
            return self.send(200, {"text": PROFILE.read_text() if PROFILE.exists() else ""})
        if path == "/api/run":
            return self.send(200, service.status())
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        if not self.host_ok():
            return self.send(403, {"error": "Unknown host"})
        if not self.authed():
            return self.send(401, {"error": "Not signed in"})
        if self.headers.get("Origin") and urlparse(self.headers["Origin"]).netloc != self.headers.get("Host"):
            return self.send(403, {"error": "Cross-site request refused"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self.send(415, {"error": "Send JSON"})  # plain forms from other sites can't set this
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            return self.send(413, {"error": "Too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "Bad JSON"})
        if not isinstance(body, dict):
            return self.send(400, {"error": "Bad JSON"})
        path = urlparse(self.path).path
        if path == "/api/job":
            return self.send(200, update_job(body))
        if path == "/api/role-contact":
            return self.send(200, role_contact(body))
        if path == "/api/company/veto":
            if body.get("name") not in read(COMPANIES, {}):
                return self.send(200, {"error": "No such account"})
            veto(str(body["name"]))
            return self.send(200, {"ok": True})
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
                from radar import accounts as A
                with locked(A.MOVES) as moves:
                    m = moves.get(body.get("id"))
                    if not m:
                        return self.send(200, {"error": "No such move"})
                    if body.get("status") in ("todo", "done", "skipped"):
                        m["status"] = body["status"]
                        m["done_at"] = now() if body["status"] == "done" else None
                    if body.get("outcome") in A.OUTCOMES + [None]:
                        m["outcome"], m["outcome_at"] = body.get("outcome"), now()
                    if "draft" in body:
                        m["draft"] = str(body["draft"])[:3000]
                    pid, status, outcome = m["person_id"], m["status"], m.get("outcome")
                with locked(A.PEOPLE) as people:  # the person's stage follows the move
                    p = people.get(pid)
                    if p:
                        order = A.PERSON_STAGES
                        target = {"replied": "replied", "conversation": "conversation", "interview": "conversation",
                                  "referral": "referred"}.get(outcome) or ("contacted" if status == "done" else None)
                        if target and order.index(target) > order.index(p.get("stage", "mapped")):
                            p["stage"] = target
                            p.setdefault("history", []).append({"stage": target, "at": now()})
                return self.send(200, {"ok": True})
            if path == "/api/person":
                from radar import accounts as A
                with locked(A.PEOPLE) as people:
                    p = people.get(body.get("id"))
                    if not p:
                        return self.send(200, {"error": "No such person"})
                    if body.get("stage") in A.PERSON_STAGES:
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
                return self.send(200, {"ok": True})
            if path == "/api/account":
                with locked(COMPANIES) as companies:
                    c = companies.get(body.get("name"))
                    if not c:
                        return self.send(200, {"error": "No such account"})
                    if body.get("tier") in ("A", "B", "C"):
                        c["tier"], c["tier_locked"] = body["tier"], True
                return self.send(200, {"ok": True})
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
            if path == "/api/test-gmail":
                from radar import inbox
                return self.send(200, inbox.test())
            if path == "/api/test-ai":
                from radar import ai
                return self.send(200, ai.test())
            if path == "/api/run":
                job = body.get("job")
                if job not in ("scan", "agent", "watchlist", "moves"):
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
    print(f"PM Radar cockpit on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
