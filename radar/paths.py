"""Where Gus keeps things: the repo, your data files and your watchlist."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS = DATA / "jobs.json"
COMPANIES = DATA / "companies.json"
EVENTS = DATA / "events.json"
PEOPLE = DATA / "people.json"
MOVES = DATA / "moves.json"
SIGNALS = DATA / "signals.json"
PROFILE = ROOT / "profile" / "profile.md"  # your CV and what you're looking for, git-ignored
WATCHLIST = ROOT / "watchlist.csv"  # yours, git-ignored; starts as a copy of watchlist.example.csv


def watchlist_path() -> Path:
    """Your watchlist, made from the example the first time it's asked for."""
    if not WATCHLIST.exists():
        shutil.copy(ROOT / "watchlist.example.csv", WATCHLIST)
    return WATCHLIST
