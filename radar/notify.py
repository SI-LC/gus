"""Notifications: phone push via ntfy (works anywhere, incl. a Raspberry Pi),
plus a desktop notification when running on a Mac.

ntfy: install the ntfy app and subscribe to a long random topic name. Anyone who knows the topic can read it, so
make it unguessable. Set the topic, server and quiet hours in the cockpit's Settings.
"""
import os
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path
from radar.store import read

ROOT = Path(__file__).resolve().parent.parent


def _cfg():
    from radar.settings import load
    return load()


def _topic():
    from radar.settings import secret
    return secret("ntfy_topic")


def _cockpit_url():
    return os.environ.get("GUS_URL") or _cfg().get("cockpit_url", "")


def quiet_now(spec=None) -> bool:
    """True inside quiet hours like "22:00-07:00"."""
    from datetime import datetime
    spec = spec if spec is not None else _cfg().get("quiet_hours", "")
    try:
        a, b = [s.strip() for s in spec.split("-")]
    except ValueError:
        return False
    now = datetime.now().strftime("%H:%M")
    return (a <= now < b) if a < b else (now >= a or now < b)


def notify(title: str, subtitle: str, message: str, url: str = "") -> None:
    if quiet_now():
        return _hold(title, subtitle, message, url)
    topic = _topic()
    if topic:
        server = os.environ.get("NTFY_SERVER") or _cfg().get("notify_ntfy_server") or "https://ntfy.sh"
        click = _cockpit_url() or url
        headers = {"Title": f"{title}: {subtitle}".encode("utf-8"), "Tags": "briefcase"}
        if click:
            headers["Click"] = click
        if url:
            headers["Actions"] = f"view, Open role, {url}"
        try:
            req = urllib.request.Request(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers)
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass
    if platform.system() != "Darwin":
        return
    if shutil.which("terminal-notifier"):
        args = ["terminal-notifier", "-title", title, "-subtitle", subtitle, "-message", message, "-sound", "Glass"]
        if url:
            args += ["-open", url]
        subprocess.run(args, check=False)
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(message)}" with title "{esc(title)}" subtitle "{esc(subtitle)}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)


HELD = ROOT / "data" / "held_notifications.json"


def _hold(*n):
    import json
    held = read(HELD, [])
    HELD.write_text(json.dumps(held + [list(n)]))


def release_held():
    """Send what was held during quiet hours, as one summary."""
    if quiet_now() or not HELD.exists():
        return
    held = read(HELD, [])
    HELD.unlink()
    if held:
        notify(f"Gus: {len(held)} while you were away", "", "; ".join(f"{h[0]} {h[1]}".strip() for h in held)[:900])


if __name__ == "__main__":
    import sys
    notify(*(sys.argv[1:] + ["", "", "", ""])[:4])
