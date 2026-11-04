"""The always-on service: the cockpit plus a scheduler that reads your Settings
every minute. One process to run on a Raspberry Pi (or any always-on machine).

    python3 -m radar.service --host 0.0.0.0

The scheduler only acts when "Run the built-in scheduler" is on in Settings, so a
Mac using the desktop app's scheduled tasks never runs things twice. "Run now"
buttons in the cockpit work either way.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import traceback
from datetime import date, datetime

from radar.scan import DATA, ROOT, load_config

STATE = DATA / "schedule_state.json"
_lock = threading.Lock()
running: dict[str, str] = {}   # job -> started at
last: dict[str, dict] = {}     # job -> {"at", "ok", "summary"}


def _state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(s):
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def _do(job):
    if job == "scan":
        out = subprocess.run(["bash", str(ROOT / "run.sh")], capture_output=True, text=True, timeout=1800, cwd=ROOT)
        return out.returncode == 0, (out.stdout + out.stderr).strip()[-300:] or "Scan finished"
    from radar import agent
    report = agent.run({"agent": "discover"}.get(job, job))
    return True, report.strip()[-300:]


def start(job: str) -> bool:
    """Run a job in the background unless it's already running."""
    with _lock:
        if job in running:
            return False
        running[job] = datetime.now().isoformat(timespec="seconds")

    def go():
        try:
            ok, summary = _do(job)
        except Exception as e:
            ok, summary = False, f"{e}"
            traceback.print_exc()
        with _lock:
            running.pop(job, None)
            last[job] = {"at": datetime.now().isoformat(timespec="seconds"), "ok": ok, "summary": summary}
            s = _state()
            s.setdefault("results", {})[job] = last[job]
            _save(s)

    threading.Thread(target=go, daemon=True).start()
    return True


def _times(spec):
    return [t.strip() for t in (spec or "").split(",") if t.strip()]


def current_slot(now: datetime, spec) -> str | None:
    """The latest time today in a list like "08:00, 13:00" that has already passed."""
    return max((t for t in _times(spec) if t <= now.strftime("%H:%M")), default=None)


def due(now: datetime, cfg: dict, s: dict) -> list[str]:
    jobs = []
    hm = now.strftime("%H:%M")
    slot = current_slot(now, cfg.get("scan_times"))
    if slot and s.get("scan") != f"{now.date()} {slot}":
        jobs.append("scan")
    slot = current_slot(now, agent_spec(now, cfg))
    if slot and s.get("agent") != f"{now.date()} {slot}":
        jobs.append("agent")
    wl_time = (_times(cfg.get("watchlist_time")) or ["07:30"])[0]
    last_wl = s.get("watchlist")
    every = cfg.get("watchlist_every_days", 3)
    if hm >= wl_time and (not last_wl or (now.date() - date.fromisoformat(last_wl)).days >= every):
        jobs.append("watchlist")
    mv_day, mv_time = cfg.get("moves_day", "Mon"), (_times(cfg.get("moves_time")) or ["07:45"])[0]
    if now.strftime("%a") == mv_day and hm >= mv_time and s.get("moves") != str(now.date()):
        jobs.append("moves")
    return jobs


def agent_spec(now: datetime, cfg: dict):
    return cfg.get("agent_times_weekends" if now.weekday() >= 5 else "agent_times_weekdays")


def scheduler():
    first = True
    while True:
        try:
            cfg = load_config()
            if cfg.get("scheduler_enabled"):
                now = datetime.now()
                s = _state()
                if first:  # after a restart, don't fire slots that passed while it was down
                    for job, spec in (("scan", cfg.get("scan_times")), ("agent", agent_spec(now, cfg))):
                        slot = current_slot(now, spec)
                        if slot and not str(s.get(job, "")).startswith(str(now.date())):
                            s[job] = f"{now.date()} {slot}"
                    _save(s)
                for job in due(now, cfg, s):
                    if start(job):
                        s = _state()
                        if job == "scan":
                            s["scan"] = f"{now.date()} {current_slot(now, cfg.get('scan_times'))}"
                        elif job == "agent":
                            s["agent"] = f"{now.date()} {current_slot(now, agent_spec(now, cfg))}"
                        else:
                            s[job] = str(now.date())
                        _save(s)
            first = False
        except Exception:
            traceback.print_exc()
        time.sleep(60)


def status() -> dict:
    s = _state()
    with _lock:
        return {"running": dict(running), "last": {**s.get("results", {}), **last},
                "scheduled": {k: s.get(k) for k in ("scan", "agent", "watchlist", "moves")},
                "enabled": bool(load_config().get("scheduler_enabled"))}


def main():
    threading.Thread(target=scheduler, daemon=True).start()
    from radar import server
    server.main()


if __name__ == "__main__":
    main()
