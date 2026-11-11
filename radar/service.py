"""The always-on service: the cockpit, plus one full run a day.

    python3 -m radar.service --host 0.0.0.0

There are three jobs, run strictly one at a time. A full run does everything in a fixed order, so the
parts can't drift out of sync; it starts at your daily time (if the scheduler is on in Settings) or from
the Full run button. Check roles is its first step on its own: one scan (run.sh), which reads every board,
feed and inbox, scores what's new and maps people at accounts with fresh news. Job sources searches Adzuna
and JSearch when they're due.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import date, datetime

from radar.matching import load_config
from radar.paths import DATA, ROOT
from radar.store import read

STATE = DATA / "schedule_state.json"
_lock = threading.Lock()
_queue: queue.Queue = queue.Queue()
running: dict[str, str] = {}   # job -> started at
last: dict[str, dict] = {}     # job -> {"at", "ok", "summary", "steps"}
progress: dict = {}            # the job running now, step by step



def _schedule_state():
    return read(STATE)


def _save_schedule_state(s):
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def _check_roles():
    """One scan, start to finish: everything in run.sh."""
    out = subprocess.run(["bash", str(ROOT / "run.sh")], capture_output=True, text=True, timeout=1800, cwd=ROOT)
    return out.returncode == 0, (out.stdout + out.stderr).strip()[-300:] or "Scan finished"


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
    """Boards for companies that arrived with a role or a careers address, then a scan so their roles are scored this run."""
    from radar import resolve
    _run_modules("links")  # a role from a job site or a web search: find the employer's board and watch it
    out = resolve.auto()
    if out["added"] or out["restored"]:
        _run_modules("scan")
    n, g, r = len(out["added"]), len(out["guesses"]), len(out["restored"])
    return True, f"{n} board{'' if n == 1 else 's'} added" + (f", {r} put back on the watchlist" if r else "") + (f", {g} to look at in Companies" if g else "")


def _run_modules(*modules):
    """Run Gus's own commands one after another. A failure is left to that command's log and the Health section."""
    for m in modules:
        subprocess.run([sys.executable, "-m", f"radar.{m}"], capture_output=True, text=True, timeout=1800, cwd=ROOT)
    return True, ""


def _counts():
    jobs = read(DATA / "jobs.json")
    return {"roles": len(jobs), "strong": sum((j.get("score") or 0) >= 8 and j.get("stage") == "new" for j in jobs.values())}


def _emails_waiting():
    return sum(e.get("status") == "new" for e in read(DATA / "emails.json").values())


def _moves_this_week():
    from radar.accounts import week_id
    return sum(m.get("week") == week_id() for m in read(DATA / "moves.json").values())


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
        ("Scan and score", _check_roles, lambda: None),
        ("Job sources", _sources, lambda: None if _sources_on(cfg) else "Turned off"),
        ("Read emails", _agent_step("inbox"), lambda: None if _emails_waiting() else "No new emails"),
        ("Discover", _agent_step("discover"), lambda: None),
        ("Find boards", _boards, lambda: None),
        ("Refresh accounts", _agent_step("watchlist"), lambda: None if _watchlist_due(cfg, s) else f"Done in the last {every} days"),
        ("Score what's new", lambda: _run_modules("triage", "people"), lambda: None),
        ("Plan moves", _agent_step("moves"), lambda: "Already planned this week" if _moves_this_week() else None),
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
    if any(x["name"] == "Refresh accounts" and x["status"] == "done" for x in progress["steps"]):
        s["watchlist"] = str(date.today())
        _save_schedule_state(s)
    after = _counts()
    new = after["roles"] - before["roles"]
    summary = f"{new} new role{'' if new == 1 else 's'}, {after['strong']} strong, {_moves_this_week()} moves this week"
    summary += f". {sources_note}" if sources_note else ""
    return ok and not stop, f"{stop}. {summary}" if stop else summary


def _run_job(job):
    return {"run": full_run, "scan": _check_roles, "sources": _sources}[job]()


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
        progress.update(job=job, steps=[])
        try:
            ok, summary = _run_job(job)
        except Exception as e:
            ok, summary = False, f"{e}"
            traceback.print_exc()
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


def scheduler():
    first = True
    while True:
        try:
            cfg = load_config()
            now = datetime.now()
            s = _schedule_state()
            if sources_due(now, cfg, s) and start("sources"):
                s["sources"] = str(now.date())
                _save_schedule_state(s)
            if cfg.get("scheduler_enabled"):
                if first and due(now, cfg, s):  # started after today's time: don't fire straight away
                    s["run"] = str(now.date())
                    _save_schedule_state(s)
                elif due(now, cfg, s) and start("run"):
                    s["run"] = str(now.date())
                    _save_schedule_state(s)
            first = False
        except Exception:
            traceback.print_exc()
        time.sleep(60)


def status() -> dict:
    s = _schedule_state()
    with _lock:
        cfg = load_config()
        return {"running": dict(running), "last": {**s.get("results", {}), **last},
                "progress": json.loads(json.dumps(progress)) if running else {},
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
