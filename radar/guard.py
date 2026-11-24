"""Keeps an agent run out of the big data files. Claude Code asks this before each file read and shell command of a
web run (radar/agent.py passes it as a hook); a refusal comes with what to use instead, so the step isn't wasted twice.

Each step of a run re-reads everything so far, so one search of data/signals.json is paid for at every step after it.
The playbooks already say to use the commands. They were advice, and advice was ignored: this is the same rule, kept.
It saves steps and nothing else: what a run may never see is ruled out in radar/agent.py's DENY, not here.
"""
from __future__ import annotations

import json
import sys

CLOSED = {
    "data/signals.json": "New signals are in your brief, and a flagged company's news is under `news` there.",
    "data/companies.json": "Use `python3 -m radar.companies known`, `show` or `set`, several names in one call.",
}


def refusal(call: dict) -> str | None:
    """Why this tool call is turned away, or None to let it through. Gus's own commands read these files themselves and pass."""
    given = call.get("tool_input") or {}
    if call.get("tool_name") == "Bash":
        text = given.get("command") or ""
        if all(part.strip().startswith("python3 -m radar.") for part in text.split("&&")) and "|" not in text and ";" not in text:
            return None
    else:
        text = f"{given.get('file_path') or ''} {given.get('path') or ''}"
    for name, instead in CLOSED.items():
        if name.split("/")[1] in text:  # by its file name, so `cd data` first changes nothing
            return f"{name} is closed to this run: it is large, and every later step would re-read what you pull from it. {instead}"
    return None


def main():
    try:
        why = refusal(json.loads(sys.stdin.read()))
    except ValueError:
        return  # nothing to judge: let the call through
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": why}}))


if __name__ == "__main__":
    main()
