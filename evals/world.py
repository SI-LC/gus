"""A fresh install in a temporary folder: the repo's own files and nothing private, running for real.

Only two things are faked, both in evals/fake: job boards and the claude command. HOME is temporary too.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "evals" / "fake"
WATCHLIST = "company,ats,slug,sector\nHarborlight,greenhouse,harborlight,ai-lab\n"
BOOT = "from radar import service; service.main()"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class World:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="gus-smoke-"))
        self.app, self.home = self.base / "app", self.base / "home"
        files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.split("\n")
        for name in filter(None, files):
            if (ROOT / name).is_file():
                target = self.app / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
        shutil.copy(self.app / "profile" / "profile.example.md", self.app / "profile" / "profile.md")
        (self.app / "watchlist.csv").write_text(WATCHLIST)
        tools = self.home / ".local" / "bin"
        tools.mkdir(parents=True)
        for tool in ("claude", "osascript"):
            shutil.copy2(FAKE / tool, tools / tool)
        kept = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # a Git hook sets these to the real repo
        self.env = {**kept, "HOME": str(self.home), "PYTHONPATH": str(FAKE), "PATH": f"{tools}:{os.environ['PATH']}"}
        for key in ("ANTHROPIC_API_KEY", "NTFY_TOPIC", "X_BEARER_TOKEN"):
            self.env.pop(key, None)
        self.server = None
        self.url = ""

    def scan(self):
        """The real scan script, as the daily run and the Check roles button run it."""
        return subprocess.run(["bash", "run.sh"], cwd=self.app, env=self.env, capture_output=True, text=True, timeout=180)

    def crashes(self) -> dict:
        """run.sh carries on past a failed step, so a crash only shows up in its logs."""
        return {p.name: p.read_text()[-600:] for p in (self.app / "data").glob("*.log") if "Traceback" in p.read_text()}

    def ai_calls(self) -> int:
        log = self.home / "claude-calls.log"
        return len(log.read_text().splitlines()) if log.exists() else 0

    def start(self):
        port = free_port()
        self.url = f"http://127.0.0.1:{port}"
        self.server = subprocess.Popen(["python3", "-c", BOOT, "--port", str(port)], cwd=self.app, env=self.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            if self.server.poll() is not None:
                raise RuntimeError(f"The cockpit stopped on start:\n{self.server.stdout.read()[-800:]}")
            try:
                urllib.request.urlopen(self.url, timeout=1).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("The cockpit didn't start within 10 seconds")

    def stop(self):
        if self.server:
            self.server.terminate()
            self.server.wait(timeout=10)
            self.server.stdout.close()
            self.server = None

    def close(self):
        self.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=10) as r:
            return json.loads(r.read())

    def post(self, path, body, headers=None):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(request, timeout=10) as r:
            return json.loads(r.read())
