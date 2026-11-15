"""deploy/details.sh, the scan's check that none of your details would be published, in a copy of the repo with a fake
Pi: your details are read here when your private files are, and from the Pi when they aren't, and the check fails
rather than passing when it can read neither."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PEOPLE = '{"p1": {"name": "Robin Quarry"}}'


class DetailsCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo, self.pi = self.tmp / "repo", self.tmp / "pi" / "gus"
        (self.repo / "deploy").mkdir(parents=True)
        for f in ("details.sh", "personal_terms.py"):
            shutil.copy(ROOT / "deploy" / f, self.repo / "deploy" / f)
        (self.pi / "data").mkdir(parents=True)
        (self.pi / "data" / "people.json").write_text(PEOPLE)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        ssh = bin_dir / "ssh"  # runs the command in the fake Pi's home; NO_PI makes it unreachable
        ssh.write_text(f'#!/bin/sh\n[ -f "{self.tmp}/NO_PI" ] && exit 255\nfor a; do cmd="$a"; done\ncd "{self.pi.parent}" && sh -c "$cmd"\n')
        ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
        self.env = {k: v for k, v in os.environ.items() if k not in ("CI", "GUS_PI")} | {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        self.addCleanup(shutil.rmtree, self.tmp)

    def check(self, published, **env):
        content = self.tmp / "content"
        content.write_text(published)
        r = subprocess.run(["bash", "deploy/details.sh", str(content)], cwd=self.repo, env=self.env | env, capture_output=True, text=True)
        return r.returncode, r.stdout

    def test_reads_your_details_from_the_pi_when_they_arent_here(self):
        code, out = self.check("print('hello Robin Quarry')")
        self.assertEqual(code, 1)
        self.assertIn("Robin Quarry", out)
        self.assertEqual(self.check("print('hello')")[0], 0)

    def test_fails_when_the_pi_cant_be_reached(self):
        (self.tmp / "NO_PI").touch()
        code, out = self.check("print('hello')")
        self.assertEqual(code, 1)
        self.assertIn("couldn't check your details", out)

    def test_your_files_here_are_used_without_asking_the_pi(self):
        (self.tmp / "NO_PI").touch()
        (self.repo / "profile").mkdir()
        (self.repo / "profile" / "profile.md").write_text("Me | me@example.org | +44 1234 567890")
        self.assertEqual(self.check("x = 'me@example.org'")[0], 1)
        self.assertEqual(self.check("x = 1")[0], 0)

    def test_on_github_it_says_it_did_not_check(self):
        (self.tmp / "NO_PI").touch()
        code, out = self.check("anything", CI="true")
        self.assertEqual(code, 0)
        self.assertIn("not checked on GitHub", out)


if __name__ == "__main__":
    unittest.main()
