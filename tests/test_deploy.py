"""The scripts that put Gus on a machine and keep it current, against pretend machines: real Git checkouts in a made-up
home folder, with curl, pkill, sleep and the system's service commands faked."""
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "update.sh"
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


class Deploy(UpdatePi):
    """deploy/deploy.sh on your own machine, with the scan, ssh and gh faked: ssh keeps what it was sent."""

    def setUp(self):
        super().setUp()
        for name, body in (("ssh", '#!/bin/sh\ncat >> "$HOME/sent"\n'), ("gh", '#!/bin/sh\necho "$*" >> "$HOME/released"\n')):
            tool = self.home / "bin" / name
            tool.write_text(body)
            tool.chmod(0o755)
        (self.home / "dev" / "deploy").mkdir()
        (self.home / "dev" / "deploy" / "scan.sh").write_text("echo scanned\n")
        (self.home / "dev" / "deploy" / "update.sh").write_text("# the update\n")
        self.commit("deploy/deploy.sh", (ROOT / "deploy" / "deploy.sh").read_text())
        self.git("pull", "-q", "origin", "main", cwd=self.app)  # the machine deploying, on main as it was

    for _name in [n for n in dir(UpdatePi) if n.startswith("test_")]:
        locals()[_name] = None

    def run_deploy(self):
        out = subprocess.run(["bash", "deploy/deploy.sh"], cwd=self.app, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        return out.returncode, out.stdout + out.stderr

    def test_it_sends_the_update_and_publishes_a_release(self):
        code, out = self.run_deploy()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.home / "sent").read_text(), "# the update\n")
        self.assertIn("release create v0.1.0", (self.home / "released").read_text())

    def test_a_merge_that_changes_the_deploy_itself_is_deployed_by_the_new_copy(self):
        dev = self.home / "dev" / "deploy"
        self.git("mv", "deploy/update.sh", "deploy/update-renamed.sh")
        (dev / "update-renamed.sh").write_text("# the renamed update\n")
        new = self.commit("deploy/deploy.sh", (ROOT / "deploy" / "deploy.sh").read_text().replace("< deploy/update.sh", "< deploy/update-renamed.sh"))
        code, out = self.run_deploy()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.home / "sent").read_text(), "# the renamed update\n")
        self.assertIn(f"On GitHub: {new}", out)
        self.assertEqual(out.count("scanned"), 1, "the scan runs once, after the pull")

    def test_work_that_is_not_committed_or_not_on_main_stops_it(self):
        (self.app / "radar" / "app.py").write_text("x = 99\n")
        code, out = self.run_deploy()
        self.assertEqual((code, (self.home / "sent").exists()), (1, False), out)
        self.assertIn("Commit your changes first", out)
        self.git("checkout", "-q", "--", ".", cwd=self.app)
        self.git("checkout", "-q", "-b", "side", cwd=self.app)
        code, out = self.run_deploy()
        self.assertEqual((code, (self.home / "sent").exists()), (1, False), out)
        self.assertIn("Deploy from main", out)


class UpdateHere(UpdatePi):
    """`gus update` on your own computer: the same script, moving to the latest release, not to a commit."""

    def setUp(self):
        super().setUp()
        (self.home / "dev" / "deploy").mkdir()
        self.good = self.release("v0.1.0", "deploy/gus.sh", 'echo restarted >> "$HOME/restarts"\n')  # stands in for the real restart
        self.git("fetch", "-q", "--tags", "origin", cwd=self.app)
        self.git("checkout", "-q", "--detach", "v0.1.0", cwd=self.app)
        self.env = {**self.env, "GUS_LOCAL": "1", "GUS_DIR": str(self.app)}

    def release(self, tag, name, text):
        sha = self.commit(name, text)
        self.git("tag", tag)
        self.git("push", "-q", "origin", tag)
        return sha

    def update(self):
        with SCRIPT.open() as script:
            out = subprocess.run(["bash", "-s"], stdin=script, env=self.env, capture_output=True, text=True, timeout=30)
        return out.returncode, out.stdout + out.stderr

    deploy = None  # the Pi's tests don't apply: nothing here is sent a commit
    for _name in [n for n in dir(UpdatePi) if n.startswith("test_")]:
        locals()[_name] = None

    def test_a_new_release_goes_live(self):
        new = self.release("v0.1.1", "radar/app.py", "x = 2\n")
        code, out = self.update()
        self.assertEqual((code, self.on_pi(), self.restarts()), (0, new, 1), out)
        self.assertIn("Gus is running v0.1.1", out)

    def test_with_no_new_release_nothing_is_touched(self):
        self.commit("radar/app.py", "x = 2\n")  # merged, but not released
        code, out = self.update()
        self.assertEqual((code, self.on_pi(), self.restarts()), (0, self.good, 0), out)
        self.assertIn("Gus is up to date (v0.1.0)", out)

    def test_a_release_that_breaks_the_cockpit_is_rolled_back(self):
        self.release("v0.1.1", "BROKEN", "x")
        code, out = self.update()
        self.assertEqual((code, self.on_pi(), self.restarts()), (1, self.good, 2), out)
        self.assertIn(f"Rolled back: Gus is running {self.good}", out)
        self.assertIn("gus logs", out)

    def test_a_copy_ahead_of_the_release_is_left_alone(self):
        ahead = self.commit("radar/app.py", "x = 5\n")
        self.git("fetch", "-q", "origin", cwd=self.app)
        self.git("checkout", "-q", "--detach", "origin/main", cwd=self.app)
        code, out = self.update()
        self.assertEqual((code, self.on_pi(), self.restarts()), (0, ahead, 0), out)
        self.assertIn("ahead of the latest release", out)

    def test_a_running_job_or_a_change_of_your_own_stops_it(self):
        self.release("v0.1.1", "radar/app.py", "x = 2\n")
        (self.home / "BUSY").write_text("")
        code, out = self.update()
        self.assertEqual((code, self.on_pi()), (1, self.good), out)
        (self.home / "BUSY").unlink()
        (self.app / "radar" / "app.py").write_text("x = 99\n")
        code, out = self.update()
        self.assertEqual((code, self.on_pi()), (1, self.good), out)
        self.assertIn("Gus has local changes", out)


MAC = platform.system() == "Darwin"


class Install(unittest.TestCase):
    """deploy/install.sh piped into bash, as the one-line install does it, in a home folder with nothing in it."""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp()).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.home, True)
        fakes = cls.home / "fakes"
        fakes.mkdir()
        called = '#!/bin/sh\necho "$(basename "$0") $*" >> "$HOME/called"\n'
        for name, body in (("launchctl", called), ("systemctl", called), ("loginctl", called), ("curl", "#!/bin/sh\nprintf 200\n"), ("sleep", "#!/bin/sh\n")):
            (fakes / name).write_text(body)
            (fakes / name).chmod(0o755)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PYTHONDONTWRITE", "PYTHONPYCACHE", "GUS_"))}
        cls.env = {**clean, "HOME": str(cls.home), "PATH": f"{fakes}:{os.environ['PATH']}", "USER": "me", "GUS_NO_OPEN": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                   "GIT_CONFIG_GLOBAL": os.devnull}
        # What GitHub would serve: this code as it stands, released as v9.9.9
        src = cls.home / "src"
        for folder in ("radar", "deploy", ".githooks"):
            shutil.copytree(ROOT / folder, src / folder, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(ROOT / "config.json", src)
        (src / ".gitignore").write_text("__pycache__/\nsettings.json\n.secrets.json\ndata/\n")
        for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "release"], ["tag", "v9.9.9"]):
            subprocess.run(["git", *args], cwd=src, env=cls.env, check=True, capture_output=True)
        cls.env["GUS_REPO"] = str(src)
        cls.gus = cls.home / "Gus"
        cls.code, cls.out = cls.install()

    @classmethod
    def install(cls, **more):
        with (ROOT / "deploy" / "install.sh").open() as script:
            done = subprocess.run(["bash"], stdin=script, env={**cls.env, **more}, capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout + done.stderr

    def run_gus(self, *args):
        done = subprocess.run([str(self.home / ".local" / "bin" / "gus"), *args], env=self.env, capture_output=True, text=True, timeout=30)
        return done.returncode, done.stdout + done.stderr

    def called(self):
        return (self.home / "called").read_text()

    def test_1_the_latest_release_is_put_in_your_home_folder_and_started(self):
        self.assertEqual(self.code, 0, self.out)
        self.assertTrue((self.gus / "radar" / "service.py").exists(), self.out)
        tag = subprocess.run(["git", "describe", "--tags"], cwd=self.gus, env=self.env, capture_output=True, text=True).stdout.strip()
        self.assertEqual(tag, "v9.9.9")
        self.assertIn("Gus is running at http://127.0.0.1:8484", self.out)
        self.assertIn("launchctl kickstart" if MAC else "systemctl --user restart gus", self.called())

    def test_2_it_starts_when_you_log_in_from_its_own_folder(self):
        service = self.home / ("Library/LaunchAgents/com.gus.cockpit.plist" if MAC else ".config/systemd/user/gus.service")
        text = service.read_text()
        self.assertIn(str(self.gus.resolve()), text)
        self.assertIn("radar.service", text)
        self.assertIn("8484", text)
        self.assertIn("KeepAlive" if MAC else "Restart=always", text)
        if not MAC:
            self.assertIn("systemctl --user enable -q gus", self.called())

    def test_3_a_new_install_catches_up_a_missed_run_and_guards_your_files(self):
        self.assertIs(json.loads((self.gus / "settings.json").read_text())["catch_up_missed_run"], True)
        hooks = subprocess.run(["git", "config", "core.hooksPath"], cwd=self.gus, env=self.env, capture_output=True, text=True).stdout.strip()
        self.assertEqual(hooks, ".githooks")
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=self.gus, env=self.env, capture_output=True, text=True).stdout, "",
                         "or gus update would refuse to run")

    def test_4_the_gus_command_answers(self):
        self.assertEqual(self.run_gus("status"), (0, "Gus v9.9.9 is running at http://127.0.0.1:8484\n"))
        code, out = self.run_gus("connect")
        self.assertIn("claude mcp add --transport http gus http://127.0.0.1:8484/mcp", out)
        self.assertEqual(json.loads(out.split("restart the app:")[1].split("\n\n")[0])["mcpServers"]["gus"]["env"]["PYTHONPATH"], str(self.gus.resolve()))
        self.assertIn("gus update", self.run_gus()[1])
        self.assertIn("Gus is up to date (v9.9.9)", self.run_gus("update")[1])

    def test_5_installing_again_keeps_what_is_there(self):
        (self.gus / "settings.json").write_text('{"run_time": "07:00"}')
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertIn(f"Using the copy of Gus in {self.gus}", out)
        self.assertEqual(json.loads((self.gus / "settings.json").read_text()), {"run_time": "07:00"})

    def test_6_a_folder_that_is_not_gus_is_never_written_over(self):
        other = self.home / "Other"
        other.mkdir()
        (other / "notes.txt").write_text("mine")
        code, out = self.install(GUS_HOME=str(other))
        self.assertEqual(code, 1)
        self.assertIn("isn't Gus", out)
        self.assertEqual([f.name for f in other.iterdir()], ["notes.txt"])

    def test_7_uninstalling_stops_it_and_leaves_your_search(self):
        code, out = self.run_gus("uninstall")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.home / ".local" / "bin" / "gus").exists())
        self.assertFalse((self.home / ("Library/LaunchAgents/com.gus.cockpit.plist" if MAC else ".config/systemd/user/gus.service")).exists())
        self.assertTrue((self.gus / "settings.json").exists())
        self.assertIn("launchctl bootout" if MAC else "systemctl --user stop gus", self.called())


if __name__ == "__main__":
    unittest.main()
