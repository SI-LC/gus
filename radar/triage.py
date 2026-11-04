"""Score untriaged roles against profile/profile.md using headless Claude Code,
then notify for strong matches.

Runs only when scan found something, so Claude is called rarely.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from radar import ai
from radar.notify import notify
from radar.scan import JOBS, ROOT, load_config
from radar.store import locked

PROMPT = (ROOT / "prompts" / "triage.md").read_text()


def main():
    profile_path = ROOT / "profile" / "profile.md"
    if not profile_path.exists():
        sys.exit("profile/profile.md missing — copy profile/profile.example.md and fill it in")
    store = json.loads(JOBS.read_text())
    todo = [j for j in store.values() if j.get("score") is None]
    if not todo:
        return
    batch = [{k: j[k] for k in ("id", "company", "title", "location", "url")} | {"description": j["description"][:6000]} for j in todo[:15]]
    prompt = PROMPT.replace("{{PROFILE}}", profile_path.read_text()).replace("{{JOBS}}", json.dumps(batch, ensure_ascii=False))
    try:
        text = ai.run(prompt, task="scoring", timeout=600)
    except Exception as e:
        sys.exit(f"scoring failed: {e}")
    if "[" not in text:
        sys.exit(f"scoring failed: unexpected reply {text.strip()[:200]}")
    results = json.loads(text[text.index("["): text.rindex("]") + 1])

    threshold = load_config()["notify_min_score"]
    alerts = []
    with locked(JOBS) as store:  # re-read: the cockpit may have changed things meanwhile
        for r in results:
            j = store.get(r["id"])
            if not j:
                continue
            j.update(score=r["score"], why=r["why"], flags=r.get("flags", []))
            if r["score"] < 5 and j.get("stage") == "new":
                j["stage"] = "skipped"
            if r["score"] >= threshold:
                alerts.append((f"{r['score']}/10 · {j['company']}", j["title"], r["why"], j["url"]))
    for a in alerts:
        notify(*a)


if __name__ == "__main__":
    main()
