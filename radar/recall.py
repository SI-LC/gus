"""Did Gus find the roles you found? Roles you spotted yourself are the answer key.

    python3 -m radar.recall add "Omnea" "Product Director" [YYYY-MM-DD]   # a role you saw somewhere, and when
    python3 -m radar.recall                                               # check every one, and say why each miss was missed
    python3 -m radar.recall --offline                                     # the same, without reading any board

Your list is data/evals/expected.csv (company, title, seen, note): add to it by hand or with `add`. Each run reports
the share Gus found (its recall), the share that reached you rather than being skipped, and for each miss the cause:
nothing reads the company, its board fell off the watchlist, your title or location rules drop the role, or it isn't
on the board. A miss is read once from the company's own board to find that cause. Reports are kept in
data/evals/recall/, so the share can be followed over time. It changes nothing in your roles.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime

from radar import jobs as J
from radar import scan
from radar.store import read

EXPECTED = scan.DATA / "evals" / "expected.csv"
REPORTS = scan.DATA / "evals" / "recall"
FIELDS = ["company", "title", "seen", "note"]
SHORT = {"pm": "product manager", "sr": "senior", "snr": "senior", "vp": "vice president", "tpm": "technical product manager"}
FILLER = {"of", "the", "and", "a", "an", "for", "in", "at", "to", "uk", "london", "remote"}
OTHER_JOB = {"design", "designer", "marketing", "engineer", "engineering", "analyst", "operations", "sales", "security", "scientist"}
ENOUGH = 0.75  # the share of the words you wrote that a title must carry to count as the same role
LISTED = ("greenhouse", "ashby", "lever", "workable", "smartrecruiters", "recruitee")  # boards whose whole list one request gives
SKIPPED_BELOW = 5  # triage skips a role scoring under this


def words(title: str) -> set:
    """The words that tell one title from another. You write "Senior PM (Growth)", the board "Senior Product Manager, Growth"."""
    text = re.sub(r"[^a-z0-9 ]", " ", str(title or "").lower())
    out = []
    for w in text.split():
        out += SHORT.get(w, w).split()
    return {w for w in out if w not in FILLER}


def same_employer(a: str, b: str) -> bool:
    """"Faculty" is "Faculty AI", and "Wise" isn't "Otherwise": one name's words start the other's."""
    x, y = J.same_company(a).split(), J.same_company(b).split()
    n = min(len(x), len(y))
    return bool(n) and x[:n] == y[:n]


def closeness(expected: str, title: str) -> tuple:
    """(share of your words the title carries, how little else it says). An exact title wins over a longer one that contains it."""
    want, have = words(expected), words(title)
    if not want or not have or (have - want) & OTHER_JOB:  # "Product Director" isn't "Product Design Director"
        return (0.0, 0.0)
    return (len(want & have) / len(want), len(want & have) / len(want | have))


def best(expected: dict, rows: list):
    """The row whose title is this role, or None. Rows are (title, anything)."""
    scored = [(closeness(expected["title"], title), n) for n, (title, _) in enumerate(rows)]
    scored = [s for s in scored if s[0][0] >= ENOUGH]
    return rows[max(scored)[1]] if scored else None


def listing(ats: str, slug: str):
    """Every job on a board as (title, place), with no filter of yours applied, or None for a board that can't be listed whole."""
    if ats == "greenhouse":
        found = scan.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").get("jobs", [])
        return [(j["title"], (j.get("location") or {}).get("name", "")) for j in found]
    if ats in LISTED:
        return [(j["title"], j["location"]) for j in scan.SOURCES[ats]("", slug)]
    return None


def why_missed(expected: dict, companies: dict, watched: dict, cfg, list_board=listing) -> str:
    """The first thing that stopped this role reaching Gus. `watched` is {company: (ats, slug)} from watchlist.csv."""
    name = next((n for n in companies if same_employer(n, expected["company"])), None)
    row = next((v for n, v in watched.items() if same_employer(n, expected["company"])), None)
    if not row:
        rec = companies.get(name) or {}
        if rec.get("board"):
            return f"its board ({rec['board']}) has fallen off the watchlist, so no scan reads it"
        if name:
            return "nothing reads this company: it's an account with no job board, and no job site listed the role"
        return "the company isn't one Gus watches, and no job site or board listed the role"
    if list_board is None:
        return f"it wasn't on {row[0]}:{row[1]} when last scanned (run without --offline to read the board and see why)"
    try:
        jobs = list_board(*row)
    except Exception as e:  # a board that won't answer is itself the reason
        return f"its board ({row[0]}:{row[1]}) didn't answer: {str(e)[:80]}"
    if jobs is None:
        return f"it's a {row[0]} board, which can't be listed whole to check"
    hit = best(expected, jobs)
    if not hit:
        return f"it isn't on its board now ({len(jobs)} jobs listed): closed, or advertised somewhere Gus doesn't read"
    title, place = hit
    if not cfg["title_include"].search(title):
        return f'your title rules drop it: nothing in Titles to include matches "{title}"'
    if scan.left_out(title, cfg, "title_exclude"):
        return f'your title rules drop it: Titles to leave out catches "{title}"'
    if not scan.matches({"title": title, "location": place, "description": ""}, cfg):
        return f'your location rules drop it: "{title}" is listed in "{place or "no place given"}"'
    return f'it\'s on the board as "{title}" and fits your rules: the next scan should bring it in'


def check(expected: list, store: dict, companies: dict, watched: dict, cfg, list_board=listing) -> list:
    """One result per role you listed: found (and whether it reached you), or missed and why."""
    out = []
    for e in expected:
        rows = [(j.get("title") or "", jid) for jid, j in store.items() if same_employer(j.get("company") or "", e["company"])]
        hit = best(e, rows)
        if not hit:
            out.append({**e, "found": False, "why": why_missed(e, companies, watched, cfg, list_board)})
            continue
        j = store[hit[1]]
        buried = j.get("stage") == "skipped"
        sources = sorted({hit[1].split(":")[0], *(a.split(":")[0] for a in j.get("also") or {})})
        out.append({**e, "found": True, "id": hit[1], "as": j.get("title"), "stage": j.get("stage"), "score": j.get("score"),
                    "sources": sources, "closed": bool(j.get("ad_closed")), "reached_you": not buried,
                    "auto_skipped": buried and (j.get("score") or 0) < SKIPPED_BELOW})
    return out


def summarise(results: list) -> dict:
    total, found = len(results), [r for r in results if r["found"]]
    causes = {}
    for r in results:
        if not r["found"]:
            causes.setdefault(re.split(r"[:(]", r["why"])[0].strip(), []).append(f"{r['company']}: {r['title']}")
    return {"roles": total, "found": len(found), "recall": len(found) / total if total else None,
            "reached_you": sum(r["reached_you"] for r in found), "skipped": sum(not r["reached_you"] for r in found),
            "missed": total - len(found), "causes": causes}


def show(results: list, s: dict) -> None:
    if not s["roles"]:
        print('No roles listed yet. Add one you\'ve seen: python3 -m radar.recall add "Company" "Title"')
        return
    print(f"\nGus found {s['found']} of the {s['roles']} roles you listed ({s['recall']:.0%}). "
          f"{s['reached_you']} reached you, {s['skipped']} were skipped, {s['missed']} were missed.\n")
    for r in sorted(results, key=lambda r: (r["found"], r.get("reached_you", False), r["company"].lower())):
        role = f"{r['company']}: {r['title']}"
        if not r["found"]:
            print(f"  ✗ {role}\n      {r['why']}")
        else:
            score = f"{r['score']}/10" if r["score"] is not None else "not scored yet"
            state = ("skipped by the score" if r["auto_skipped"] else "skipped") if not r["reached_you"] else r["stage"]
            print(f"  {'✓' if r['reached_you'] else '~'} {role}\n      as \"{r['as']}\": {state}, {score}, from {', '.join(r['sources'])}"
                  + (", ad now closed" if r["closed"] else ""))
    if s["causes"]:
        print("\nWhy roles were missed:")
        for cause, roles in sorted(s["causes"].items(), key=lambda c: -len(c[1])):
            print(f"  {len(roles)}  {cause}")


def load_expected() -> list:
    if not EXPECTED.exists():
        return []
    with EXPECTED.open(newline="") as f:
        return [{k: (r.get(k) or "").strip() for k in FIELDS} for r in csv.DictReader(f) if (r.get("company") or "").strip() and (r.get("title") or "").strip()]


def add(company: str, title: str, seen: str = "", note: str = "") -> bool:
    """One more role on your list, unless it's there already."""
    have = load_expected()
    if any(same_employer(r["company"], company) and words(r["title"]) == words(title) for r in have):
        return False
    EXPECTED.parent.mkdir(parents=True, exist_ok=True)
    new = not EXPECTED.exists()
    with EXPECTED.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        w.writerow([company.strip(), title.strip(), seen or datetime.now().strftime("%Y-%m-%d"), note])
    return True


def run(offline: bool = False) -> dict:
    with scan.watchlist_path().open(newline="") as f:
        watched = {r["company"]: (r["ats"], r["slug"]) for r in csv.DictReader(f)}
    results = check(load_expected(), read(scan.JOBS), read(scan.DATA / "companies.json"), watched, scan.load_config(),
                    None if offline else listing)
    s = summarise(results)
    show(results, s)
    if s["roles"]:
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / f"{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.json").write_text(json.dumps({"summary": s, "results": results}, indent=2, ensure_ascii=False))
    return s


def main():
    args = sys.argv[1:]
    if args[:1] == ["add"] and len(args) >= 3:
        print("Added." if add(*args[1:4]) else "That role is already on your list.")
    elif not args or args == ["--offline"]:
        run(offline="--offline" in args)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
