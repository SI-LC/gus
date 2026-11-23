"""The signals inbox, for the discover agent: what's waiting, and marking a whole list as looked at in one go.

    python3 -m radar.signals new                 # the signals nobody has looked at yet
    python3 -m radar.signals mark '[{"id": "...", "status": "processed", "company": "Acme", "event_id": "..."}, {"id": "...", "status": "ignored"}]'

An agent's every step re-reads its whole conversation, so searching the file for new signals and editing them one at a
time is what made a run long. These read once and write once.
"""
from __future__ import annotations

import json
import sys

from radar.paths import SIGNALS
from radar.schema import SIGNAL_STATUS
from radar.store import locked, read

SHOWN = ["source", "kind", "title", "text", "url", "date", "name", "handle", "company"]
SETTABLE = ["status", "company", "job_id", "event_id"]  # what looking at a signal decides; the item itself is never rewritten


def waiting() -> list[dict]:
    """New signals as the agent needs them: the whole item, without the housekeeping."""
    return [{"id": sid, **{k: s[k] for k in SHOWN if s.get(k) not in (None, "")}}
            for sid, s in read(SIGNALS, {}).items() if s.get("status") == "new"]


def mark(items: list[dict]) -> dict:
    """Set what each signal turned out to be. Answers {id: its new status, or why it wasn't saved}."""
    out = {}
    with locked(SIGNALS) as signals:
        for item in items if isinstance(items, list) else []:
            sid = item.get("id") if isinstance(item, dict) else None
            if sid not in signals:
                out[str(sid)] = "no such signal"
            elif extra := [k for k in item if k not in ("id", *SETTABLE)]:
                out[sid] = f"not saved: only {', '.join(SETTABLE)} can be set, not {', '.join(extra)}"
            elif item.get("status") not in SIGNAL_STATUS:
                out[sid] = f"not saved: status must be one of {', '.join(SIGNAL_STATUS)}"
            else:
                signals[sid].update({k: item[k] for k in SETTABLE if item.get(k) not in (None, "")})
                out[sid] = item["status"]
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "new"
    if cmd == "new":
        print(json.dumps(waiting(), ensure_ascii=False))
    elif cmd == "mark":
        try:
            items = json.loads(sys.argv[2])
        except (IndexError, ValueError):
            sys.exit("mark takes one JSON list: '[{\"id\": \"...\", \"status\": \"processed\"}]'")
        print(json.dumps(mark(items), ensure_ascii=False))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
