"""What a run is doing this moment, for the cockpit's progress strip: one line in data/doing.txt, overwritten as it goes.

The scan runs as separate processes (run.sh), so a file is how each part tells the cockpit where it's got to.
"""
from __future__ import annotations

import sys

from radar.paths import DATA

NOW = DATA / "doing.txt"


def say(text: str) -> None:
    try:
        DATA.mkdir(exist_ok=True)
        NOW.write_text(text.strip()[:200])
    except OSError:  # never let progress reporting break a scan
        pass


def read() -> str:
    try:
        return NOW.read_text().strip()
    except OSError:
        return ""


def clear() -> None:
    NOW.unlink(missing_ok=True)


if __name__ == "__main__":
    say(" ".join(sys.argv[1:]))
