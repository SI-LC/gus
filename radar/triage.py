"""Score untriaged roles against profile/profile.md using headless Claude Code,
then notify for strong matches.

Runs on every scan and scores whatever is waiting, so the model is only called when there's a new role.
"""
from __future__ import annotations

import json
import sys

from radar import ai, doing, salary
from radar.notify import notify
from radar.matching import load_config
from radar.paths import JOBS, ROOT
from radar.store import locked, read

PROMPT = (ROOT / "prompts" / "triage.md").read_text()


MAX_BATCHES = 8  # 15 roles each


def usable(r, ids):
    """Anything else leaves the role unscored for the next run: a bad reply must never skip a role or score another."""
    return (isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"] in ids
            and type(r.get("score")) is int and 1 <= r["score"] <= 10
            and isinstance(r.get("why"), str) and bool(r["why"].strip()))


class ScoringFailed(Exception):
    pass


def ask(profile_path, todo, task="scoring", chars=6000, also="", timeout=600):
    """Score up to 15 roles and return the usable results. Saves nothing, so radar.evals and first run can use it too."""
    batch = [{k: j[k] for k in ("id", "company", "title", "location", "url")} | {"description": (j.get("description") or "")[:chars]} for j in todo[:15]]
    prompt = PROMPT.replace("{{PROFILE}}", profile_path.read_text()).replace("{{JOBS}}", json.dumps(batch, ensure_ascii=False)) + also
    try:
        text = ai.run(prompt, task=task, timeout=timeout)
    except Exception as e:
        raise ScoringFailed(f"scoring failed: {e}") from e
    try:
        results = json.loads(text[text.index("["): text.rindex("]") + 1])
    except ValueError as e:
        raise ScoringFailed(f"scoring failed: unexpected reply {text.strip()[:200]}") from e
    ids = {b["id"] for b in batch}
    return [r for r in results if usable(r, ids)]


def score_batch(profile_path, todo):
    try:
        results = ask(profile_path, todo)
    except ScoringFailed as e:
        sys.exit(str(e))

    cfg = load_config()
    threshold = cfg["notify_min_score"]
    alerts = []
    with locked(JOBS) as store:  # re-read: the cockpit may have changed things meanwhile
        for r in results:
            j = store.get(r["id"])
            if not j:
                continue
            flags = r.get("flags")
            flags = flags if isinstance(flags, list) else []
            score, note = salary.apply(j, r["score"], cfg)
            j.update(score=score, why=r["why"], flags=flags + [note] if note else flags)
            if note:
                j["score_before_salary"] = r["score"]
            if score < 5 and j.get("stage") == "new" and j.get("source") != "manual":  # a role you added yourself is yours to skip
                j["stage"] = "skipped"
            if score >= threshold:
                alerts.append((f"{score}/10 · {j['company']}", j["title"], r["why"], j["url"]))
    from radar import events
    events.sync()
    for a in alerts:
        notify(*a)
    events.alert()  # after the role alerts, so a role event whose role was just pushed isn't pushed twice


def main():
    profile_path = ROOT / "profile" / "profile.md"
    if not profile_path.exists():
        sys.exit("profile/profile.md missing: copy profile/profile.example.md and fill it in")
    seen = set()
    for _ in range(MAX_BATCHES):  # score everything waiting, not just what this scan found
        store = read(JOBS)
        todo = [j for j in store.values() if j.get("score") is None and j["id"] not in seen]
        if not todo:
            return
        doing.say(f"Scoring new roles with AI: {len(todo)} waiting, {min(15, len(todo))} at a time")
        seen |= {j["id"] for j in todo[:15]}  # a role Claude skips isn't retried forever
        score_batch(profile_path, todo)


if __name__ == "__main__":
    main()
