"""Score untriaged roles against profile/profile.md using headless Claude Code,
then notify for strong matches.

Runs on every scan and scores whatever is waiting, so the model is only called when there's a new role.

    python3 -m radar.triage                 # every role with no score
    python3 -m radar.triage <id> <id>       # those too, scored again. Their old score stays until the new one arrives
"""
from __future__ import annotations

import json
import sys
import time

from radar import ai, changes, doing, salary
from radar.jobs import same_company
from radar.notify import notify
from radar.matching import load_config
from radar.paths import COMPANIES, JOBS, ROOT
from radar.store import locked, read

PROMPT = (ROOT / "prompts" / "triage.md").read_text()


BUDGET = 15 * 60  # seconds a run spends scoring, 15 roles at a time. A batch takes about half a minute, so a night that
#                   brings hundreds of roles is scored in one run; the limit is there so a slow night still ends inside
#                   the half hour radar.service gives this step. What's left waits for the next run.


def usable(r, ids):
    """Anything else leaves the role unscored for the next run: a bad reply must never skip a role or score another."""
    return (isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"] in ids
            and type(r.get("score")) is int and 1 <= r["score"] <= 10
            and isinstance(r.get("why"), str) and bool(r["why"].strip()))


def favourites() -> set:
    """The companies you marked as ones you love: a low score there is shown to you, never skipped for you."""
    return {same_company(n) for n, c in read(COMPANIES, {}).items() if isinstance(c, dict) and c.get("favourite") and not c.get("vetoed")}


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


def score_batch(profile_path, todo) -> int:
    """Score up to 15 roles and save what came back. Returns how many were scored."""
    try:
        results = ask(profile_path, todo)
    except ScoringFailed as e:
        sys.exit(str(e))

    cfg = load_config()
    threshold = cfg["notify_min_score"]
    alerts, loved, scored = [], favourites(), 0
    with locked(JOBS) as store:  # re-read: the cockpit may have changed things meanwhile
        for r in results:
            j = store.get(r["id"])
            if not j:
                continue
            flags = r.get("flags")
            flags = flags if isinstance(flags, list) else []
            score, note = salary.apply(j, r["score"], cfg)
            j.update(score=score, why=r["why"], flags=flags + [note] if note else flags)
            scored += 1
            j.pop("score_before_salary", None)  # a role scored again may no longer be marked down
            if note:
                j["score_before_salary"] = r["score"]
            yours = j.get("source") == "manual" or same_company(j.get("company")) in loved  # one you added, or at a favourite: yours to skip
            if score < 5 and j.get("stage") == "new" and not yours:  # no history entry: history is what you did
                j["stage"] = "skipped"
                changes.append(changes.diff(r["id"], {"stage": "new"}, {"stage": "skipped"}, "triage", f"scored {score}/10"), JOBS)
            if score >= threshold:
                alerts.append((j["company"], j["title"], f"Scores {score} of 10. {r['why']}", j["url"]))
    from radar import events
    events.sync()
    for a in alerts:
        notify(*a)
    events.alert()  # after the role alerts, so a role event whose role was just pushed isn't pushed twice
    return scored


def main(again=()):
    """Score every role with no score, and the roles in `again` whatever they scored before, those first."""
    again = set(again)
    profile_path = ROOT / "profile" / "profile.md"
    if not profile_path.exists():
        sys.exit("profile/profile.md missing: copy profile/profile.example.md and fill it in")
    seen, scored, started = set(), 0, time.monotonic()
    while time.monotonic() - started < BUDGET:  # score everything waiting, not just what this scan found
        store = read(JOBS)
        todo = [j for j in store.values() if (j.get("score") is None or j["id"] in again) and j["id"] not in seen]
        if not todo:
            break
        todo.sort(key=lambda j: j["id"] not in again)  # the ones asked for come first
        doing.say(f"Scoring roles with AI: {len(todo)} waiting, {min(15, len(todo))} at a time")
        seen |= {j["id"] for j in todo[:15]}  # a role Claude skips isn't retried forever
        scored += score_batch(profile_path, todo)
    print(f"{scored} scored")  # radar.service reads this for what it tells you
    return scored


if __name__ == "__main__":
    main(sys.argv[1:])
