"""An install in a temporary folder: the repo's own files and nothing private, running for real. new=True is brand new:
no profile, settings or watchlist, as on the day someone installs it. Otherwise it's already set up for made-up searches.

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
STARTER = "company,ats,slug,sector\nHarborlight,greenhouse,harborlight,software\n"
PROFILE = "# Candidate profile\n\n## What I want\n- Made-up target roles\n"
# A company found with no careers address, whose board Gus guessed from its name: it waits for a person in Companies
COMPANIES = {"Lanternfield": {"status": "candidate", "added": "2026-11-01",
                              "board_guess": {"board": "greenhouse:lanternfield", "url": "https://job-boards.greenhouse.io/lanternfield",
                                              "open_roles": 3, "found": "2026-11-01"}}}
SETTINGS = {"title_include": "product manager\nproduct owner", "location_include": "london\nremote (exactly)",
            "where_groups": "London: london\nRemote: remote, anywhere\nElsewhere: *",
            "role_kinds": "Forward deployed: forward deployed\nProduct: *"}
BOOT = "from radar import service; service.main()"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class World:
    def __init__(self, new=False, signed_out=False):
        self.base = Path(tempfile.mkdtemp(prefix="gus-smoke-"))
        self.app, self.home = self.base / "app", self.base / "home"
        files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.split("\n")
        for name in filter(None, files):
            if (ROOT / name).is_file():
                target = self.app / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
        (self.app / "starter-boards.csv").write_text(STARTER)
        if not new:
            (self.app / "profile" / "profile.md").write_text(PROFILE)
            (self.app / "watchlist.csv").write_text(WATCHLIST)
            (self.app / "settings.json").write_text(json.dumps(SETTINGS))
            (self.app / "data").mkdir(exist_ok=True)
            (self.app / "data" / "companies.json").write_text(json.dumps(COMPANIES))
        tools = self.home / ".local" / "bin"
        tools.mkdir(parents=True)
        for tool in ("claude", "osascript"):
            shutil.copy2(FAKE / tool, tools / tool)
        kept = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # a Git hook sets these to the real repo
        self.env = {**kept, "HOME": str(self.home), "PYTHONPATH": str(FAKE), "PATH": f"{tools}:{os.environ['PATH']}"}
        for key in ("ANTHROPIC_API_KEY", "NTFY_TOPIC", "X_BEARER_TOKEN"):
            self.env.pop(key, None)
        if signed_out:
            (self.home / "signed-out").touch()
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
