"""The one way to the JSON data files: `read` for a look, `locked` for a change.

Every module reads through `read`, so a missing or half-written file means the same thing everywhere (the default), and
the scanner, the scoring steps and the cockpit never overwrite each other's changes."""
from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(path: Path, default=None):
    """with locked(JOBS) as data: mutate data in place; it's saved on exit."""
    path.parent.mkdir(exist_ok=True)
    with open(path.with_suffix(path.suffix + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else ({} if default is None else default)
        yield data
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(path)


def read(path: Path, default=None):
    """The file's contents, or the default ({} unless given) when it's missing or not yet valid JSON."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {} if default is None else default
