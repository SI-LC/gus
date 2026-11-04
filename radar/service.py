"""The always-on service: the cockpit, plus one full radar run a day.

    python3 -m radar.service --host 0.0.0.0

There are two jobs. A full run does everything in a fixed order, so the parts can't drift
out of sync; it starts at your daily time (if the scheduler is on in Settings) or from the
Run radar button. A quick check only scans and scores.
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

from radar.scan import DATA, ROOT, load_config

STATE = DATA / "schedule_state.json"
_lock = threading.Lock()
_queue: queue.Queue = queue.Queue()
running: dict[str, str] = {}   # job -> started at
last: dict[str, dict] = {}     # job -> {"at", "ok", "summary", "steps"}
progress: dict = {}            # the job running now, step by step


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(s):
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def _scan():
    out = subprocess.run(["bash", str(ROOT / "run.sh")], capture_output=True, text=True, timeout=1800, cwd=ROOT)
    return out.returncode == 0, (out.stdout + out.stderr).strip()[-300:] or "Scan finished"


def _command(*modules):
    for m in modules:
        subprocess.run([sys.executable, "-m", f"radar.{m}"], capture_output=True, text=True, timeout=1800, cwd=ROOT)
    return True, ""


def _counts():
    jobs = read_json(DATA / "jobs.json")
    return {"roles": len(jobs), "strong": sum((j.get("score") or 0) >= 8 and j.get("stage") == "new" for j in jobs.values())}


def _moves_this_week():
    from radar.accounts import week_id
    return sum(m.get("week") == week_id() for m in read_json(DATA / "moves.json").values())


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
    cfg, s = load_config(), _state()
    every = cfg.get("watchlist_every_days", 3)
    return [
        ("Scan and score", _scan, lambda: None),
        ("Read emails", _agent_step("inbox"), lambda: None if cfg.get("source_gmail") else "Gmail is off in Settings"),
        ("Discover", _agent_step("discover"), lambda: None),
        ("Refresh accounts", _agent_step("watchlist"), lambda: None if _watchlist_due(cfg, s) else f"Done in the last {every} days"),
        ("Score what's new", lambda: _command("triage", "people", "dashboard"), lambda: None),
        ("Plan moves", _agent_step("moves"), lambda: "Already planned this week" if _moves_this_week() else None),
    ]


def full_run():
    from radar import ai
    before, stop = _counts(), None
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
            good, _ = fn()
            step["status"] = "done" if good else "failed"
            ok = ok and good
        except ai.BudgetReached as e:  # nothing after this can use AI either
            stop = f"Stopped: {e}"
            step.update(status="skipped", detail=stop)
        except Exception as e:  # one failed step shouldn't lose the rest of the run
            step.update(status="failed", detail=str(e)[:200])
            ok = False
            traceback.print_exc()
    s = _state()
    if any(x["name"] == "Refresh accounts" and x["status"] == "done" for x in progress["steps"]):
        s["watchlist"] = str(date.today())
        _save(s)
    after = _counts()
    new = after["roles"] - before["roles"]
    summary = f"{new} new role{'' if new == 1 else 's'}, {after['strong']} strong, {_moves_this_week()} moves this week"
    return ok and not stop, f"{stop}. {summary}" if stop else summary


def _do(job):
    return full_run() if job == "run" else _scan()


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
            ok, summary = _do(job)
        except Exception as e:
            ok, summary = False, f"{e}"
            traceback.print_exc()
        with _lock:
            running.pop(job, None)
            last[job] = {"at": datetime.now().isoformat(timespec="seconds"), "ok": ok, "summary": summary,
                         "steps": list(progress.get("steps", []))}
            s = _state()
            s.setdefault("results", {})[job] = last[job]
            _save(s)


def _times(spec):
    return [t.strip() for t in (spec or "").split(",") if t.strip()]


def run_time(cfg) -> str | None:
    return (_times(cfg.get("run_time")) or [None])[0]


def due(now: datetime, cfg: dict, s: dict) -> bool:
    """One full run a day, at your daily time."""
    t = run_time(cfg)
    return bool(t and now.strftime("%H:%M") >= t and s.get("run") != str(now.date()))


def scheduler():
    first = True
    while True:
        try:
            cfg = load_config()
            if cfg.get("scheduler_enabled"):
                now = datetime.now()
                s = _state()
                if first and due(now, cfg, s):  # started after today's time: don't fire straight away
                    s["run"] = str(now.date())
                    _save(s)
                elif due(now, cfg, s) and start("run"):
                    s["run"] = str(now.date())
                    _save(s)
            first = False
        except Exception:
            traceback.print_exc()
        time.sleep(60)


def status() -> dict:
    s = _state()
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
