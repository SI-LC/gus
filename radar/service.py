"""The always-on service: the cockpit, plus one full run a day.

    python3 -m radar.service --host 0.0.0.0

There are five jobs, run strictly one at a time. A full run does everything in a fixed order, so the
parts can't drift out of sync; it starts at your daily time (if the scheduler is on in Settings) or from
the Full run button. Check roles is its first step on its own: one scan (run.sh), which reads every board,
feed and inbox, scores what's new and maps people at accounts with fresh news. Job sources searches Adzuna
and JSearch when they're due. Scoring scores the roles with no score, and any you or Claude asked to have scored again, and
reads no board: it starts when a role is corrected or when it's asked for. Catch-up reads the boards a vendor held back ("slow down") once its hold has run out, so a
refusal in the morning costs a few hours and not the day.
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import date, datetime

from radar import boardhealth, doing, scan, web
from radar.matching import load_config
from radar.paths import DATA, JOBS, MOVES, ROOT
from radar.store import read

STATE = DATA / "schedule_state.json"
_lock = threading.Lock()
_queue: queue.Queue = queue.Queue()
running: dict[str, str] = {}   # job -> started at
last: dict[str, dict] = {}     # job -> {"at", "ok", "summary", "steps"}
progress: dict = {}            # the job running now, step by step
_again: set = set()            # roles asked to be scored again, until the scoring job takes them
_find: set = set()             # roles whose employer ad was asked for, until the links job takes them



def _schedule_state():
    return read(STATE)


def _save_schedule_state(s):
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def _check_roles():
    """One scan, start to finish: everything in run.sh. Says what it found, as the full run does."""
    before = _counts()
    # an hour: a vendor that has said "slow down" is asked at web.SLOWER_PAUSE, and a big board on one takes minutes
    out = subprocess.run(["bash", str(ROOT / "run.sh")], capture_output=True, text=True, timeout=3600, cwd=ROOT)
    if out.returncode:
        return False, "Didn't finish: " + ((out.stdout + out.stderr).strip()[-250:] or f"exit code {out.returncode}")
    after = _counts()
    new = after["roles"] - before["roles"]
    return True, f"{new} new role{'' if new == 1 else 's'}, {after['strong']} strong in your inbox" + _unread_note()


def _catch_up():
    """Read the boards a vendor held back on the last scan, now its hold has run out, and score anything new."""
    before = _counts()["roles"]
    out = subprocess.run([sys.executable, "-m", "radar.scan", "--held"], capture_output=True, text=True, timeout=1800, cwd=ROOT)
    if _counts()["roles"] > before:
        _run_modules("triage")
    return out.returncode == 0, (out.stdout.strip().splitlines() or ["Didn't finish"])[0] + _unread_note()


def score(ids=()) -> bool:
    """Score these roles again, and any role with no score, without reading a board. Asked while scoring is under way, they
    join it."""
    with _lock:
        _again.update(ids)
    return start("score")


def find_ad(ids=()) -> bool:
    """Look for these roles' employer ads now. Asked while a search is under way, they wait for it and then run."""
    with _lock:
        _find.update(ids)
    return start("links")


def _links():
    """The "Find employer's ad" job: radar.links on the roles asked for, which scores again any whose ad changed them."""
    said = "Nothing to look for"
    for _ in range(5):
        with _lock:
            ids = sorted(_find)
            _find.clear()
        if not ids:
            break
        out = subprocess.run([sys.executable, "-m", "radar.links", *ids], capture_output=True, text=True, timeout=1800, cwd=ROOT)
        if out.returncode:
            return False, "Didn't finish: " + ((out.stderr or out.stdout).strip()[-250:] or f"exit code {out.returncode}")
        board = re.search(r"found (\d+) on company sites", out.stdout)
        web = re.search(r"found (\d+) employer pages", out.stdout)
        n = int(board[1]) if board else 0
        n += int(web[1]) if web else 0
        said = f"Found the employer's ad for {n} of {len(ids)} role{'' if len(ids) == 1 else 's'}" if len(ids) > 1 else \
            ("Found the employer's ad" if n else "No employer's ad found. The role keeps its link")
    return True, said


def _score():
    """The scoring job: radar.triage on what's waiting. A role corrected or asked for while it runs is scored before it ends."""
    scored = 0
    for _ in range(5):
        with _lock:
            ids = sorted(_again)
            _again.clear()
        if not ids and not _unscored():
            break
        out = subprocess.run([sys.executable, "-m", "radar.triage", *ids], capture_output=True, text=True, timeout=1800, cwd=ROOT)
        if out.returncode:
            return False, "Didn't finish: " + ((out.stderr or out.stdout).strip()[-250:] or f"exit code {out.returncode}")
        found = re.search(r"(\d+) scored\s*$", out.stdout)
        if not found or not int(found[1]):
            break  # nothing came back: asking again now would get the same
        scored += int(found[1])
    left = _unscored()
    return True, (f"{scored} role{'' if scored == 1 else 's'} scored" if scored else "No role was scored") + (f", {left} still waiting" if left else "")


def _unscored() -> int:
    return sum(j.get("score") is None for j in read(JOBS).values())


def _unread_note() -> str:
    """Boards Gus hasn't been able to read for days, for the end of a run's summary: roles there could be missing."""
    note = boardhealth.note()
    return f". {note}" if note else ""


def _sources():
    """Job sources: fetch whatever is due, then score it. Run on its own schedule, and as a step of the full run."""
    if not _sources_on(load_config()):
        return True, "Job sources are turned off"
    before = _counts()["roles"]
    out = subprocess.run([sys.executable, "-m", "radar.sources"], capture_output=True, text=True, timeout=3600, cwd=ROOT)
    if _counts()["roles"] > before:  # nothing new means nothing to score
        _run_modules("triage")
    _run_modules("links")  # strong roles: swap job-site links for the employer's own ad
    lines = [line for line in out.stdout.splitlines() if line.strip() and not line.endswith("turned off")]
    return out.returncode == 0, ". ".join(lines)[-300:] or "Nothing to do"


def _sources_on(cfg) -> bool:
    return bool(cfg.get("source_adzuna") or cfg.get("source_jsearch"))


def _boards():
    """Boards for companies that arrived with a role or a careers address, then a scan of those boards so their roles are scored this run."""
    from radar import resolve
    _run_modules("links")  # a role from a job site or a web search: find the employer's board and, if Gus kept the role, read it
    out = resolve.auto()
    if out["added"] or out["restored"]:
        _run_modules("scan --new")  # only the boards just added: Check roles has asked every other one this run
    n, g, r = len(out["added"]), len(out["guesses"]), len(out["restored"])
    return True, f"{n} board{'' if n == 1 else 's'} added" + (f", {r} put back in your companies" if r else "") + (f", {g} to look at in Companies" if g else "")


def _run_modules(*modules):
    """Run Gus's own commands one after another. A failure is left to that command's log and the Health section."""
    for m in modules:
        subprocess.run([sys.executable, "-m", *f"radar.{m}".split()], capture_output=True, text=True, timeout=1800, cwd=ROOT)
    return True, ""


def _counts():
    jobs = read(JOBS)
    return {"roles": len(jobs), "strong": sum((j.get("score") or 0) >= 8 and j.get("stage") == "new" for j in jobs.values())}


def _emails_waiting():
    return sum(e.get("status") == "new" for e in read(DATA / "emails.json").values())


def _has_gmail_login(cfg):
    from radar.settings import secret
    return bool((cfg.get("gmail_address") or "").strip() and secret("gmail_app_password"))


def _emails_skip(cfg):
    """Why the run isn't reading emails, or None to read them. "No new emails" is only true when Gus can read them."""
    if not cfg.get("source_gmail"):
        return "Email reading is off (Settings → Email)"
    if not _has_gmail_login(cfg):
        return "No Gmail address and app password yet (Settings → Email)"
    return None if _emails_waiting() else "No new emails"


def _moves_this_week():
    from radar.accounts import week_id
    return sum(m.get("week") == week_id() for m in read(MOVES).values())


def _watchlist_due(cfg, s):
    last_run = s.get("watchlist")
    return not last_run or (date.today() - date.fromisoformat(last_run)).days >= cfg.get("watchlist_every_days", 3)


def _agent_step(name):
    def go():
        from radar import agent
        return True, agent.run(name).strip()[-300:]
    return go


def steps():
    """The full run, in order. Each step: (label, function, why it's skipped, or None to run it)."""
    cfg, s = load_config(), _schedule_state()
    every = cfg.get("watchlist_every_days", 3)
    return [
        ("Check roles", _check_roles, lambda: None),
        ("Job sources", _sources, lambda: None if _sources_on(cfg) else "Turned off"),
        ("Read emails", _agent_step("inbox"), lambda: _emails_skip(cfg)),
        ("Find new companies", _agent_step("discover"), lambda: None),
        ("Find job boards", _boards, lambda: None),
        ("Catch up on your companies", _agent_step("watchlist"), lambda: None if _watchlist_due(cfg, s) else f"Done in the last {every} days"),
        ("Find people", lambda: _run_modules("triage", "people"), lambda: None),
        ("Pick who to contact", _agent_step("moves"), lambda: "Already planned this week" if _moves_this_week() else None),
    ]


def full_run():
    from radar import ai
    before, stop, sources_note = _counts(), None, ""
    plan = steps()
    progress["steps"] = [{"name": n, "status": "waiting", "detail": ""} for n, _, _ in plan]
    ok = True
    for step, (_, fn, skip_if) in zip(progress["steps"], plan):
        reason = stop or skip_if()
        if reason:
            step.update(status="skipped", detail=reason)
            continue
        step["status"] = "running"
        doing.say(step["name"])  # the step's own parts say more as they go
        try:
            good, detail = fn()
            step["status"] = "done" if good else "failed"
            if step["name"] == "Job sources":
                step["detail"] = sources_note = detail
            ok = ok and good
        except (ai.BudgetReached, ai.LoginExpired) as e:  # nothing after this can use AI either
            stop = f"Stopped: {e}"
            step.update(status="skipped", detail=stop)
        except Exception as e:  # one failed step shouldn't lose the rest of the run
            step.update(status="failed", detail=str(e)[:200])
            ok = False
            traceback.print_exc()
    s = _schedule_state()
    if any(x["name"] == "Catch up on your companies" and x["status"] == "done" for x in progress["steps"]):
        s["watchlist"] = str(date.today())
        _save_schedule_state(s)
    boardhealth.keep()  # the run may have found boards for companies that had none
    after = _counts()
    new = after["roles"] - before["roles"]
    summary = f"{new} new role{'' if new == 1 else 's'}, {after['strong']} strong in your inbox, {_moves_this_week()} moves this week"
    summary += _unread_note()
    summary += f". {sources_note}" if sources_note else ""
    return ok and not stop, f"{stop}. {summary}" if stop else summary


def _run_job(job):
    return {"run": full_run, "scan": _check_roles, "sources": _sources, "catchup": _catch_up, "score": _score, "links": _links}[job]()


def start(job: str) -> bool:
    """Queue a job. Jobs run strictly one at a time, so two agents never edit the same files."""
    with _lock:
        if job in running:
            return False
        running[job] = datetime.now().isoformat(timespec="seconds")
    _queue.put(job)
    return True


def worker():
    while True:
        job = _queue.get()
        progress.clear()
        progress.update(job=job, steps=[], started=datetime.now().isoformat(timespec="seconds"))
        doing.say("Starting")
        try:
            ok, summary = _run_job(job)
        except Exception as e:
            ok, summary = False, f"{e}"
            traceback.print_exc()
        doing.clear()
        with _lock:
            running.pop(job, None)
            last[job] = {"at": datetime.now().isoformat(timespec="seconds"), "ok": ok, "summary": summary,
                         "steps": list(progress.get("steps", []))}
            s = _schedule_state()
            s.setdefault("results", {})[job] = last[job]
            _save_schedule_state(s)


def _times(spec):
    return [t.strip() for t in (spec or "").split(",") if t.strip()]


def run_time(cfg) -> str | None:
    return (_times(cfg.get("run_time")) or [None])[0]


def due(now: datetime, cfg: dict, s: dict) -> bool:
    """One full run a day, at your daily time."""
    t = run_time(cfg)
    return bool(t and now.strftime("%H:%M") >= t and s.get("run") != str(now.date()))


def sources_due(now: datetime, cfg: dict, s: dict) -> bool:
    """Once a day, whether or not the daily run is on. Each source then decides if it's due, so an extra check costs nothing."""
    t = (_times(cfg.get("sources_run_time")) or [None])[0]
    return bool(_sources_on(cfg) and cfg.get("sources_auto") and t and now.strftime("%H:%M") >= t and s.get("sources") != str(now.date()))


def catch_up_due(cfg: dict, s: dict) -> str:
    """The moment a vendor's hold ran out, when boards are still waiting on it and they haven't been read since; else ""."""
    ended = web.hold_ended() if cfg.get("scheduler_enabled") else ""
    return ended if ended and s.get("catch_up") != ended and scan.waiting() else ""


def tick(first: bool, now: datetime = None) -> None:
    """One look at the clock. `first` is the look made as Gus starts."""
    cfg = load_config()
    now = now or datetime.now()
    s = _schedule_state()
    if (ended := catch_up_due(cfg, s)) and start("catchup"):
        s["catch_up"] = ended
        _save_schedule_state(s)
    if sources_due(now, cfg, s) and start("sources"):
        s["sources"] = str(now.date())
        _save_schedule_state(s)
    if cfg.get("scheduler_enabled") and due(now, cfg, s):
        # Started after today's time: a server that was restarted lets it go, a laptop that was off catches up
        if (first and not cfg.get("catch_up_missed_run")) or start("run"):
            s["run"] = str(now.date())
            _save_schedule_state(s)


def scheduler():
    first = True
    while True:
        try:
            tick(first)
            first = False
        except Exception:
            traceback.print_exc()
        time.sleep(60)


def status() -> dict:
    s = _schedule_state()
    with _lock:
        cfg = load_config()
        return {"running": dict(running), "last": {**s.get("results", {}), **last},
                "progress": {**json.loads(json.dumps(progress)), "now": doing.read()} if running else {},
                "run_time": run_time(cfg), "enabled": bool(cfg.get("scheduler_enabled"))}


def main():
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    from radar import server
    server.main()


if __name__ == "__main__":
    # Run through the imported module: `python -m` would otherwise make a second copy, with its
    # own queue, and the cockpit's buttons would queue jobs that nothing ever picks up.
    from radar import service
    service.main()
