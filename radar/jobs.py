"""One role, however many places list it.

Every source mints its own id (`gh:acme:123`, `adzuna:456`, `email:<id>:1`), so the same ad found twice would be two
records. `find_match` is the one check every writer makes before adding a role; `absorb` records the second sighting
on the record Gus already has. `dedupe` merges what slipped through before.

    python3 -m radar.jobs dedupe            # show the duplicates, change nothing
    python3 -m radar.jobs dedupe --apply    # merge them
    python3 -m radar.jobs add '{"company": "Acme", "title": "Product Manager", "location": "London", "url": "https://acme.example/jobs/1"}'
                                            # how an agent adds a role it found on the web: checked, never written by hand
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from radar.matching import load_config, matches
from radar.paths import JOBS
from radar.store import locked, read

STAGE_ORDER = ["new", "skipped", "closed", "shortlisted", "preparing", "applied", "interviewing"]
TRACKING = re.compile(r"^(utm_|gh_src$|src$|ref$|source$|lever-source|ashby_jid$|fbclid$|gclid$)")


def same_company(s) -> str:
    s = re.sub(r"[(),.'’]", " ", (s or "").lower().replace("&", "and"))
    s = re.sub(r"\b(limited|ltd|plc|inc|llc|llp|sarl|gmbh|group|holdings?|uk|europe|emea|careers|company|the|corp|corporation)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def same_title(s) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", (s or "").lower())
    s = re.sub(r"\bjob in .*$|\s[-–—|]\s*(london|uk|remote|hybrid|united kingdom).*$", " ", s)
    s = re.sub(r"\bsr\b\.?", "senior", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def same_url(url) -> str:
    """The address without tracking tags, a trailing slash or a fragment. Query keys that pick the job stay."""
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if not p.netloc:
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k.lower())])
    # capitals in the path never pick a different job, and job sites copy them differently: /Omnea/ and /omnea/ are one board
    return urlunsplit(("https", p.netloc.lower().removeprefix("www."), p.path.rstrip("/").lower(), query, ""))


def board_of(jid: str) -> str:
    """`gh:acme` for `gh:acme:123`: the two ids on one board are two roles, whatever their titles."""
    return ":".join(str(jid).split(":")[:2])


ONE_EMPLOYER = {"google deepmind": "google", "deepmind": "google"}  # a job site says Google, Google's own board says DeepMind


def role_key(j: dict) -> tuple:
    company = same_company(j.get("company"))
    return ONE_EMPLOYER.get(company, company), same_title(j.get("title"))


def same_vacancy(a: dict, b: dict) -> bool:
    """The employer's own number for the vacancy is on both: an ad taken down and put up again gets a new id and keeps it."""
    return bool(a.get("ref")) and a.get("ref") == b.get("ref")


def is_live(j: dict) -> bool:
    return not j.get("ad_closed") and j.get("stage") not in ("closed", "skipped")


def still_open(j: dict) -> bool:
    """Its ad is still up, whatever you did with it. A role you skipped is still that role when another site lists it, so it
    mustn't come back as new; one whose ad has closed can't be the ad just found."""
    return not j.get("ad_closed") and j.get("stage") != "closed"


def aliases(store: dict) -> dict:
    """Every id a stored role has been seen under, including its own."""
    out = {}
    for jid, j in store.items():
        out[jid] = jid
        for aid in j.get("also") or {}:
            out.setdefault(aid, jid)
    return out


def find_match(store: dict, role: dict, alias: dict | None = None) -> str | None:
    """The id of the stored role this one is, or None. By id (or an id it was absorbed under), then by address, then by
    company and title among roles whose ad is still open, found somewhere other than this one's own board, or on it under
    the same vacancy number: the ad put up again."""
    alias = aliases(store) if alias is None else alias
    rid = role.get("id")
    if rid in alias:
        return alias[rid]
    url = same_url(role.get("url"))
    key = role_key(role)
    by_key = None
    for jid, j in store.items():
        if url and (same_url(j.get("url")) == url or any(same_url(a.get("url")) == url for a in (j.get("also") or {}).values())):
            return jid
        if by_key is None and key[0] and key[1] and still_open(j) and role_key(j) == key and (board_of(jid) != board_of(rid) or same_vacancy(j, role)):
            by_key = jid
    return by_key


def absorb(existing: dict, role: dict, now: str) -> None:
    """A second sighting of a role Gus has: remembered on the record, which fills any gaps from it."""
    rid = role.get("id")
    if rid and rid != existing.get("id"):
        existing.setdefault("also", {})[rid] = {k: v for k, v in (("url", role.get("url")), ("source", role.get("source")), ("seen", now)) if v}
    existing["last_seen"] = now
    for k in ("location", "posted", "salary_max", "ref"):
        if not existing.get(k) and role.get(k):
            existing[k] = role[k]
    if role.get("description") and (not existing.get("description") or
                                    (existing.get("text_kind") == "summary" and role.get("text_kind", "posting") == "posting")):
        existing["description"] = role["description"]  # the employer's own words score better than a job site's précis
        existing["text_kind"] = role.get("text_kind", "posting")


def rank(j: dict) -> tuple:
    """Which copy to keep: the furthest along, then the scored one, then the earliest seen."""
    return (STAGE_ORDER.index(j.get("stage")) if j.get("stage") in STAGE_ORDER else 0, j.get("score") is not None,
            bool(j.get("history")), -len(str(j.get("first_seen") or "9")), str(j.get("first_seen") or "9"))


def groups(store: dict) -> list[list[str]]:
    """Ids that are one role: the same address, or the same company and title on different boards, or on one board under
    the same vacancy number, among live roles."""
    parent = {jid: jid for jid in store}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    by_url, by_key = {}, {}
    for jid, j in store.items():
        for u in [j.get("url")] + [a.get("url") for a in (j.get("also") or {}).values()]:
            u = same_url(u)
            if u:
                if u in by_url:
                    union(jid, by_url[u])
                by_url.setdefault(u, jid)
        key = role_key(j)
        if is_live(j) and key[0] and key[1]:
            by_key.setdefault(key, []).append(jid)
    for ids in by_key.values():
        boards = [board_of(i) for i in ids]
        if len(set(boards)) == len(boards):  # a title two live roles share on one board is two roles: too ambiguous to merge by title
            for other in ids[1:]:
                union(ids[0], other)
        reposted = {}
        for i in ids:
            if store[i].get("ref"):
                union(i, reposted.setdefault((board_of(i), store[i]["ref"]), i))
    out = {}
    for jid in store:
        out.setdefault(find(jid), []).append(jid)
    return [g for g in out.values() if len(g) > 1]


def merge(store: dict, group: list[str], now: str) -> tuple[str, list[str]]:
    """Fold a group into its best copy. Returns the kept id and the ids that went."""
    keep = max(group, key=lambda jid: (rank(store[jid]), jid))
    gone = [jid for jid in group if jid != keep]
    for jid in gone:
        j = store.pop(jid)
        absorb(store[keep], {**j, "id": jid}, j.get("last_seen") or now)
        for aid, a in (j.get("also") or {}).items():
            store[keep].setdefault("also", {}).setdefault(aid, a)
        if (j.get("first_seen") or "9") < (store[keep].get("first_seen") or "9"):
            store[keep]["first_seen"] = j["first_seen"]
        for k in ("note", "folder", "why", "flags"):
            if not store[keep].get(k) and j.get(k):
                store[keep][k] = j[k]
        if j.get("score") is not None and store[keep].get("score") is None:
            store[keep]["score"], store[keep]["why"] = j["score"], j.get("why")
        for pid, when in (j.get("contacts") or {}).items():
            store[keep].setdefault("contacts", {}).setdefault(pid, when)
        store[keep]["history"] = sorted((store[keep].get("history") or []) + (j.get("history") or []), key=lambda h: h.get("at") or "")
        if not store[keep]["history"]:
            del store[keep]["history"]
    store[keep]["last_seen"] = max(filter(None, [store[keep].get("last_seen"), now]))
    return keep, gone


def repoint(events: dict, moves: dict, keep: str, gone: list[str]) -> None:
    """A merged copy's role event and any move on it follow the kept role."""
    kept_eid = f"role:{keep}"
    for jid in gone:
        eid = f"role:{jid}"
        if eid in events:
            if kept_eid not in events:
                events[kept_eid] = {**events.pop(eid), "job_id": keep, "source": f"job:{keep}"}
            else:
                del events[eid]
        for m in moves.values():
            if m.get("event_id") == eid:
                m["event_id"] = kept_eid


def dedupe(apply: bool = False) -> list[tuple[str, list[str]]]:
    from radar.accounts import MOVES
    from radar.events import EVENTS

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done = []
    with locked(JOBS) as store:
        found = groups(store)
        if not apply:
            return [(max(g, key=lambda jid: (rank(store[jid]), jid)), sorted(g)) for g in found]
        with locked(EVENTS) as events, locked(MOVES) as moves:
            for g in found:
                keep, gone = merge(store, g, now)
                repoint(events, moves, keep, gone)
                done.append((keep, gone))
    return done


def web_id(role: dict) -> str:
    """The same ad found on the web twice gets the same id, so adding it again changes nothing."""
    slug = lambda s: re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return f"web:{slug(same_company(role['company']))[:30]}:{slug(role['title'])[:40]}-{hashlib.sha256(same_url(role['url']).encode()).hexdigest()[:6]}"


def manual_id(company: str, title: str) -> str:
    """A role you add by hand has no ad to take an id from: the same company and title is the same role, so adding it twice changes nothing."""
    slug = re.sub(r"[^a-z0-9]+", "-", (same_company(company) or str(company).lower())).strip("-")[:30] or "company"
    return f"manual:{slug}:{hashlib.sha256(same_title(title).encode()).hexdigest()[:8]}"


def add(rows: list, source: str = "web") -> list[tuple[str, str]]:
    """Roles an agent found by reading the web, each put through the checks every other source gets. Returns (what happened,
    id or reason) per role: "added"; "have" when Gus has this ad already, from any source, and the sighting is recorded on
    it; "no" when it's malformed or outside the candidate's titles and locations. An agent never decides what is a duplicate."""

    cfg, now, out = load_config(), datetime.now(timezone.utc).isoformat(timespec="seconds"), []
    with locked(JOBS) as store:
        alias = aliases(store)
        for r in rows:
            if not isinstance(r, dict) or not all(isinstance(r.get(k), str) and r[k].strip() for k in ("company", "title", "url")):
                out.append(("no", "needs company, title and url"))
                continue
            if not r["url"].startswith("https://"):
                out.append(("no", "the url must be the ad's own https address"))
                continue
            role = {"id": web_id(r), "company": r["company"].strip(), "title": r["title"].strip(), "url": r["url"].strip(),
                    "location": str(r.get("location") or "").strip(), "posted": r.get("posted") or None,
                    "description": str(r.get("description") or "").strip()}
            if (have := find_match(store, role, alias)) is not None:
                if have != role["id"]:
                    absorb(store[have], {**role, "source": source}, now)
                    alias[role["id"]] = have
                store[have]["last_seen"] = now
                out.append(("have", have))
            elif not matches(role, cfg):
                out.append(("no", f"not one of the candidate's titles and locations: {role['title']} | {role['location'] or 'no place given'}"))
            else:
                store[role["id"]] = {**role, "first_seen": now, "last_seen": now, "stage": "new", "score": None, "why": None, "source": source}
                alias[role["id"]] = role["id"]
                out.append(("added", role["id"]))
    return out


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "add":
        try:
            rows = json.loads(sys.argv[2])
        except ValueError:
            sys.exit("add takes one JSON object, or a list of them")
        for what, detail in add(rows if isinstance(rows, list) else [rows]):
            print({"added": "added", "have": "already have it, as", "no": "not added:"}[what], detail)
        return
    if len(sys.argv) < 2 or sys.argv[1] != "dedupe":
        print(__doc__)
        return
    apply = "--apply" in sys.argv
    store = read(JOBS)
    for keep, group in dedupe(apply):
        j = store.get(keep) or {}
        others = [i for i in group if i != keep]
        print(f"{'merged' if apply else 'would merge'} {', '.join(others)} into {keep}  ({j.get('company')}: {str(j.get('title'))[:60]})")
    if not apply:
        print("Nothing changed. Run again with --apply to merge.", file=sys.stderr)


if __name__ == "__main__":
    main()
