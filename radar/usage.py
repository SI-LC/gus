"""Record what each AI run and paid API call costs, for the cockpit.

data/usage.jsonl has one line per call. On a Claude subscription `cost_usd` is what the
run would have cost at API prices: a measure of how hard it uses your allowance, not a charge.
Runs that don't go through radar/ai.py (desktop app scheduled tasks) aren't recorded.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from radar.paths import DATA

LOG = DATA / "usage.jsonl"


def record(**row) -> None:
    DATA.mkdir(exist_ok=True)
    row["at"] = datetime.now().isoformat(timespec="seconds")
    with LOG.open("a") as f:  # one short append per call, so runs can't overwrite each other
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def rows() -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a half-written line shouldn't hide the rest
    return out


def _total(items) -> dict:
    return {"cost_usd": round(sum(r.get("cost_usd") or 0 for r in items), 4), "runs": len(items),
            "searches": sum(r.get("searches") or 0 for r in items),
            "tokens": sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in items)}


def today_cost() -> float:
    today = str(date.today())
    return sum(r.get("cost_usd") or 0 for r in rows() if r.get("kind") == "ai" and r["at"][:10] == today)


def summary() -> dict:
    all_rows = rows()
    today = date.today()
    ai = [r for r in all_rows if r.get("kind") == "ai"]
    x = [r for r in all_rows if r.get("kind") == "x"]
    since = lambda items, start: [r for r in items if r["at"][:10] >= str(start)]
    month_start = today.replace(day=1)
    by_task = defaultdict(list)
    for r in since(ai, month_start):
        by_task[r.get("task") or "other"].append(r)
    days = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        days.append({"date": str(d), "ai": _total([r for r in ai if r["at"][:10] == str(d)])["cost_usd"],
                     "x": _total([r for r in x if r["at"][:10] == str(d)])["cost_usd"]})
    return {
        "ai": {"today": _total(since(ai, today)), "week": _total(since(ai, today - timedelta(days=6))), "month": _total(since(ai, month_start))},
        "x": {"today": _total(since(x, today)), "month": _total(since(x, month_start)),
              "posts_month": sum(r.get("posts") or 0 for r in since(x, month_start))},
        "by_task": {k: _total(v) for k, v in sorted(by_task.items())},
        "days": days,
        "recent": all_rows[-12:][::-1],
        "first": all_rows[0]["at"] if all_rows else None,
    }
