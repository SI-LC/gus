"""Warm paths: match your LinkedIn connections against your accounts.

Get the file from LinkedIn: Settings → Data privacy → Get a copy of your data →
Connections. Then upload Connections.csv in the cockpit (Settings → Network), or:

    python3 -m radar.network import ~/Downloads/Connections.csv
    python3 -m radar.network match

Only name, profile link, company, position and date are kept (emails are dropped).
Matches become "warm" people at that account. Everything stays in data/.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from datetime import date

from radar.accounts import COMPANIES, PEOPLE, person_id, read, tier
from radar.scan import DATA
from radar.store import locked

NETWORK = DATA / "network.json"
SUFFIX = re.compile(r"\b(ltd|limited|inc|llc|plc|gmbh|sas|bv|ag|co|corp|corporation|group|holdings|technologies|technology|labs|hq|uk)\b\.?", re.I)


def norm(company: str) -> str:
    s = (company or "").lower().replace("&", "and")
    s = re.sub(r"\.(ai|io|com|co|dev|so|app)\b", "", s)
    s = SUFFIX.sub("", s)
    s = re.sub(r"\bai\s*$", "", s.strip())  # "Acme AI" = "Acme"
    return re.sub(r"[^a-z0-9]+", "", s)


def parse(text: str) -> list[dict]:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lower().startswith("first name")), None)
    if start is None:
        raise ValueError("That doesn't look like LinkedIn's Connections.csv (no 'First Name' header)")
    rows = []
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        name = f"{r.get('First Name', '').strip()} {r.get('Last Name', '').strip()}".strip()
        if not name:
            continue
        rows.append({"name": name, "url": (r.get("URL") or "").strip(), "company": (r.get("Company") or "").strip(),
                     "position": (r.get("Position") or "").strip(), "connected_on": (r.get("Connected On") or "").strip()})
    return rows


def import_text(text: str) -> int:
    rows = parse(text)
    NETWORK.write_text(json.dumps({"imported": str(date.today()), "connections": rows}, indent=2, ensure_ascii=False))
    return len(rows)


def match() -> list[dict]:
    """Connections working at watched/maybe accounts become warm people there."""
    net = read(NETWORK, {}).get("connections", [])
    companies = read(COMPANIES, {})
    index = {norm(n): n for n, c in companies.items() if tier(c) in ("A", "B", "C") and norm(n)}
    hits = []
    with locked(PEOPLE) as people:
        for c in net:
            acct = index.get(norm(c["company"]))
            if not acct:
                continue
            pid = person_id(c["name"], acct)
            rec = people.setdefault(pid, {"name": c["name"], "company": acct, "stage": "mapped", "added": str(date.today())})
            rec.update(role=rec.get("role") or c["position"], url=rec.get("url") or c["url"], platform="linkedin",
                       warm=True, warm_how=f"1st-degree LinkedIn connection since {c['connected_on']}".strip(),
                       evidence=rec.get("evidence") or f"Your connection; {c['position']} at {c['company']}",
                       kind=rec.get("kind") or ("peer_pm" if re.search(r"product", c["position"], re.I) else "warm"))
            hits.append({"account": acct, "name": c["name"], "position": c["position"]})
    return hits


RANK = {"in_touch": 0, "direct": 1, "confirmed": 2, "same_company": 3, "alumni_bridge": 4, "shared_employer": 5, "investor": 6}


def paths_for(person: dict, connections: list, account: dict, my_employers: list) -> list[dict]:
    """Known routes to one person, best first. Pure, so it can be tested without any data files.

    LinkedIn doesn't expose 2nd-degree connections, so everything except "direct" and
    "confirmed" is an inferred route, not a proven link. `via` is who you'd actually ask.
    """
    out = []
    name = (person.get("name") or "").strip().lower()
    account_key = norm(person.get("company"))
    past = {norm(c) for c in person.get("past_companies") or []} - {""}
    mine = {norm(c): c for c in my_employers if norm(c)}
    investors = {norm(i): i for i in account.get("investors") or [] if norm(i)}

    if person.get("stage") in ("replied", "conversation", "referred"):  # closer than any degree
        out.append({"kind": "in_touch", "hops": 1, "via": "", "detail": "You're already talking: they've replied to you"})
    if person.get("degree"):  # what you saw on their LinkedIn profile
        out.append({"kind": "confirmed", "hops": person["degree"], "via": person.get("via") or "",
                    "detail": f"You confirmed {person['degree']}{'st' if person['degree'] == 1 else 'nd' if person['degree'] == 2 else 'rd+'} on LinkedIn"
                              + (f", via {person['via']}" if person.get("via") else "")})
    for c in connections:
        key = norm(c.get("company"))
        if c["name"].strip().lower() == name:
            out.append({"kind": "direct", "hops": 1, "via": "", "url": c.get("url"), "detail": f"Your connection since {c.get('connected_on') or 'unknown'}"})
        elif key and key == account_key:
            out.append({"kind": "same_company", "hops": 2, "via": c["name"], "url": c.get("url"),
                        "detail": f"{c['name']} ({c.get('position') or 'unknown role'}) works there too"})
        elif key and key in past:
            out.append({"kind": "alumni_bridge", "hops": 2, "via": c["name"], "url": c.get("url"),
                        "detail": f"{c['name']} works at {c['company']}, where {person.get('name')} used to work"})
        elif key and key in investors:
            out.append({"kind": "investor", "hops": 3, "via": c["name"], "url": c.get("url"),
                        "detail": f"{c['name']} works at {investors[key]}, an investor in {person.get('company')}"})
    for key in past & set(mine):
        out.append({"kind": "shared_employer", "hops": 2, "via": "", "detail": f"Also worked at {mine[key]}, so you may share contacts"})
    out.sort(key=lambda p: (RANK[p["kind"]], p["hops"]))
    return out


def distance(paths: list[dict]) -> dict:
    """What to show: the best route, and how sure we are of it."""
    if not paths:
        return {"hops": None, "label": "3rd+", "certain": False, "paths": []}
    confirmed = next((p for p in paths if p["kind"] in ("in_touch", "direct", "confirmed")), None)
    best = confirmed or paths[0]
    hops = best["hops"]
    label = "In touch" if best["kind"] == "in_touch" else {1: "1st", 2: "2nd"}.get(hops, "3rd+")
    return {"hops": hops, "label": label if confirmed else f"~{label}", "certain": bool(confirmed), "paths": paths[:5]}


def distances(people: dict | None = None) -> dict:
    from radar.settings import load
    people = people if people is not None else read(PEOPLE, {})
    conns = read(NETWORK, {}).get("connections", [])
    companies = read(COMPANIES, {})
    mine = [x.strip() for x in (load().get("my_employers") or "").splitlines() if x.strip()]
    return {pid: distance(paths_for(p, conns, companies.get(p.get("company"), {}), mine)) for pid, p in people.items()}


def summary() -> dict:
    net = read(NETWORK, {})
    people = read(PEOPLE, {})
    warm = [p for p in people.values() if p.get("warm")]
    return {"imported": net.get("imported"), "connections": len(net.get("connections", [])),
            "warm_people": len(warm), "warm_accounts": len({p["company"] for p in warm})}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "match"
    if cmd == "import":
        print(f"{import_text(open(sys.argv[2], encoding='utf-8-sig').read())} connections imported")
        cmd = "match"
    if cmd == "match":
        hits = match()
        for h in hits:
            print(f"  ★ {h['account']}: {h['name']} ({h['position']})")
        print(f"{len(hits)} warm paths across {len({h['account'] for h in hits})} accounts")


if __name__ == "__main__":
    main()
