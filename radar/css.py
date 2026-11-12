"""Build the cockpit's stylesheet with Tailwind.

    python3 -m radar.css          # radar/cockpit.css + the classes in radar/cockpit.html -> radar/static/cockpit.css

Run it after changing radar/cockpit.css, or a class in radar/cockpit.html. It needs Tailwind's standalone program
(`tailwindcss`, from github.com/tailwindlabs/tailwindcss/releases) on your PATH or in ~/.local/bin. Only the machine
you write code on needs it: the built file is committed, so where Gus runs installs nothing. The built file's first
line records what it was built from, and the tests fail when that no longer matches.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

from radar.paths import ROOT

SOURCE = ROOT / "radar" / "cockpit.css"
PAGE = ROOT / "radar" / "cockpit.html"
BUILT = ROOT / "radar" / "static" / "cockpit.css"


def stamp() -> str:
    """What the built file depends on: the source, and every class the page names. A change to the page's script alone
    doesn't alter it."""
    classes = sorted({c for found in re.findall(r'class="([^"]*)"', PAGE.read_text()) for c in found.split()})
    return hashlib.sha256((SOURCE.read_text() + "\n" + " ".join(classes)).encode()).hexdigest()[:16]


def built_from() -> str:
    """The stamp the built file carries, or an empty string."""
    m = re.match(r"/\* built from (\w+) ", BUILT.read_text()[:80]) if BUILT.exists() else None
    return m[1] if m else ""


def main():
    tool = shutil.which("tailwindcss") or shutil.which("tailwindcss", path=str(Path.home() / ".local" / "bin"))
    if not tool:
        sys.exit("Tailwind isn't installed. Download the standalone `tailwindcss` program for this machine from "
                 "github.com/tailwindlabs/tailwindcss/releases and put it on your PATH.")
    out = subprocess.run([tool, "--input", str(SOURCE), "--minify", "--cwd", str(ROOT / "radar")], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(out.stderr.strip()[-2000:])
    BUILT.write_text(f"/* built from {stamp()} by python3 -m radar.css: edit radar/cockpit.css, not this file */\n{out.stdout.strip()}\n")
    print(f"Built {BUILT.relative_to(ROOT)} ({BUILT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
