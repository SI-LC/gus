"""deploy/update-pi.sh against a pretend Pi: a real Git checkout, with curl, pkill and sleep faked."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "update-pi.sh"
FAKE_CURL = """#!/bin/sh
# Answers as the cockpit would. A BROKEN or NO_DATA file in the checkout makes it unhealthy; BUSY means a job is running.
app="$HOME/gus"
case "$*" in
  *api/run*) [ -f "$HOME/BUSY" ] && echo '{"running": {"scan": 1}}' || echo '{"running": {}}' ;;
  *api/state*) [ -f "$app/NO_DATA" ] && echo '{}' || echo '{"jobs": []}' ;;
  *) [ -f "$app/BROKEN" ] && printf 500 || printf 200 ;;
esac
"""


class UpdatePi(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.app, self.origin = self.home / "gus", self.home / "origin.git"
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        for name, body in (("curl", FAKE_CURL), ("pkill", "#!/bin/sh\necho restarted >> \"$HOME/restarts\"\n"), ("sleep", "#!/bin/sh\n")):
            tool = bin_dir / name
            tool.write_text(body)
            tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PYTHONDONTWRITE", "PYTHONPYCACHE"))}  # a Git hook sets GIT_* to the real repo
        self.env = {**clean, "HOME": str(self.home), "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.home)
        self.git("clone", "-q", str(self.origin), str(self.home / "dev"), cwd=self.home)
        (self.home / "dev" / "radar").mkdir()
        (self.home / "dev" / ".gitignore").write_text("__pycache__/\n")  # as in the real repo: compiling writes these
        self.good = self.commit("radar/app.py", "x = 1\n")
        self.git("clone", "-q", str(self.origin), str(self.app), cwd=self.home)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.home / "dev", env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, name, text):
        (self.home / "dev" / name).write_text(text)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "change")
        self.git("push", "-q", "origin", "HEAD:main")
        return self.git("rev-parse", "--short", "HEAD")

    def deploy(self, target):
        with SCRIPT.open() as script:
            out = subprocess.run(["bash", "-s", target], stdin=script, env=self.env, capture_output=True, text=True, timeout=30)
        return out.returncode, out.stdout + out.stderr

    def on_pi(self):
        return self.git("rev-parse", "--short", "HEAD", cwd=self.app)

    def restarts(self):
        file = self.home / "restarts"
        return len(file.read_text().splitlines()) if file.exists() else 0

    def test_a_good_commit_goes_live(self):
        new = self.commit("radar/app.py", "x = 2\n")
        code, out = self.deploy(new)
        self.assertEqual((code, self.on_pi(), self.restarts()), (0, new, 1), out)
        self.assertIn(f"Pi is running {new}", out)

    def test_a_commit_that_breaks_the_cockpit_is_rolled_back(self):
        code, out = self.deploy(self.commit("BROKEN", "x"))
        self.assertEqual((code, self.on_pi(), self.restarts()), (1, self.good, 2), out)
        self.assertIn(f"Rolled back: the Pi is running {self.good}", out)

    def test_a_cockpit_that_loads_but_has_no_data_is_rolled_back(self):
        code, out = self.deploy(self.commit("NO_DATA", "x"))
        self.assertEqual((code, self.on_pi()), (1, self.good), out)

    def test_code_that_doesnt_compile_is_rolled_back_before_any_restart(self):
        code, out = self.deploy(self.commit("radar/app.py", "def broken(:\n"))
        self.assertEqual((code, self.on_pi()), (1, self.good), out)
        self.assertEqual(self.restarts(), 1)  # only the restart that brings the old code back

    def test_rolling_back_then_deploying_a_fix_works(self):
        self.deploy(self.commit("BROKEN", "x"))
        self.git("rm", "-q", "BROKEN")
        self.git("commit", "-q", "-m", "fix")
        self.git("push", "-q", "origin", "HEAD:main")
        fixed = self.git("rev-parse", "--short", "HEAD")
        code, out = self.deploy(fixed)
        self.assertEqual((code, self.on_pi()), (0, fixed), out)

    def test_a_running_job_stops_the_deploy_before_anything_changes(self):
        (self.home / "BUSY").write_text("")
        code, out = self.deploy(self.commit("radar/app.py", "x = 3\n"))
        self.assertEqual((code, self.on_pi(), self.restarts()), (1, self.good, 0), out)
        self.assertIn("scan is running", out)

    def test_local_changes_on_the_pi_stop_the_deploy_and_survive(self):
        (self.app / "radar" / "app.py").write_text("x = 99\n")
        code, out = self.deploy(self.commit("radar/app.py", "x = 3\n"))
        self.assertEqual((code, self.on_pi()), (1, self.good), out)
        self.assertEqual((self.app / "radar" / "app.py").read_text(), "x = 99\n")


if __name__ == "__main__":
    unittest.main()
