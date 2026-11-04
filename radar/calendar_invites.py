"""Pick job-search meetings out of calendar invites that arrive by email.

Invites are .ics files. A meeting is only kept if it looks related to your search: it
names a tracked account or person, or sounds like an interview. Anything else is dropped
without being saved.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from radar.accounts import COMPANIES, PEOPLE, read
from radar.network import norm
from radar.scan import DATA

MEETINGS = DATA / "meetings.json"
INVITE_SUBJECT = re.compile(r"^\s*(updated invitation|invitation|canceled event|cancelled event|accepted|declined|tentatively accepted|new event|meeting)\b", re.I)
INVITE_SENDERS = ("calendar-notification@google.com", "calendar.google.com", "outlook.com", "office365.com", "calendly.com", "cal.com")
SEARCH_WORDS = re.compile(r"\b(interview|recruit\w*|screen(ing)?|hiring|intro(ductory)?|exploratory|product (manager|lead)|\bPM\b|chat with|meet the team|onsite|panel|take.?home)\b", re.I)


def is_invite(sender: str, subject: str) -> bool:
    addr = parseaddr(sender)[1].lower()
    return bool(INVITE_SUBJECT.search(subject)) or any(s in addr for s in INVITE_SENDERS)


def _when(value: str, params: str):
    value = value.strip()
    try:
        if "T" not in value:
            return datetime.strptime(value[:8], "%Y%m%d").date().isoformat()
        d = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if value.endswith("Z"):
        return d.replace(tzinfo=timezone.utc).isoformat()
    m = re.search(r"TZID=([^;:]+)", params)
    try:
        return d.replace(tzinfo=ZoneInfo(m[1])).isoformat() if m else d.isoformat()
    except (KeyError, ValueError):  # Outlook's Windows zone names aren't IANA names
        return d.isoformat()


def _text(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def parse_ics(text: str) -> list[dict]:
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()  # undo line folding
    method, events, ev = "", [], None
    for line in lines:
        name, _, value = line.partition(":")
        key, _, params = name.partition(";")
        key = key.upper()
        if key == "METHOD" and ev is None:
            method = value.strip().upper()
        elif key == "BEGIN" and value.strip() == "VEVENT":
            ev = {"attendees": []}
        elif key == "END" and value.strip() == "VEVENT" and ev is not None:
            ev["cancelled"] = method == "CANCEL" or ev.get("status") == "CANCELLED"
            if ev.get("uid") and ev.get("start"):
                events.append(ev)
            ev = None
        elif ev is not None:
            if key == "UID":
                ev["uid"] = value.strip()
            elif key == "SUMMARY":
                ev["title"] = _text(value)[:160]
            elif key == "DTSTART":
                ev["start"] = _when(value, params)
            elif key == "DTEND":
                ev["end"] = _when(value, params)
            elif key == "LOCATION":
                ev["location"] = _text(value)[:200]
            elif key == "STATUS":
                ev["status"] = value.strip().upper()
            elif key in ("ORGANIZER", "ATTENDEE"):
                cn = re.search(r'CN="?([^";:]+)', params)
                who = {"name": (cn[1].strip() if cn else ""), "email": value.replace("mailto:", "").replace("MAILTO:", "").strip().lower()}
                if key == "ORGANIZER":
                    ev["organizer"] = who
                else:
                    ev["attendees"].append(who)
    return events


def relevance(ev: dict, companies: dict, people: dict):
    """Why this meeting belongs to the search, or None to drop it."""
    who = [ev.get("organizer") or {}] + ev.get("attendees", [])
    names = " ".join(w.get("name", "") for w in who)
    title = ev.get("title", "")
    for pid, p in people.items():
        if p.get("name") and p["name"].lower() in f"{title} {names}".lower():
            return {"account": p.get("company"), "person_id": pid, "why": f"With {p['name']}"}
    domains = {norm(w.get("email", "").split("@")[-1].rsplit(".", 1)[0]) for w in who}
    for name, c in companies.items():
        if c.get("status") not in ("watched", "maybe"):
            continue
        n = norm(name)
        if n and (n in domains or re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", f"{title} {names}", re.I)):
            return {"account": name, "person_id": None, "why": f"Mentions {name}"}
    if SEARCH_WORDS.search(title):
        return {"account": None, "person_id": None, "why": "Sounds like an interview"}
    return None


def keep(events: list[dict]) -> int:
    from radar.store import locked
    companies, people = read(COMPANIES, {}), read(PEOPLE, {})
    kept = 0
    with locked(MEETINGS) as meetings:
        for ev in events:
            if ev["uid"] in meetings:  # an update or cancellation of a meeting already kept
                meetings[ev["uid"]].update(start=ev["start"], end=ev.get("end"), title=ev.get("title"),
                                           location=ev.get("location"), cancelled=ev["cancelled"])
                continue
            why = relevance(ev, companies, people)
            if not why:
                continue  # not job-related: dropped, not stored
            meetings[ev["uid"]] = {"title": ev.get("title"), "start": ev["start"], "end": ev.get("end"),
                                   "location": ev.get("location"), "organizer": (ev.get("organizer") or {}).get("name"),
                                   "cancelled": ev["cancelled"], **why}
            kept += 1
    if kept:
        with locked(PEOPLE) as ppl:  # a booked meeting means it's a real conversation
            for m in read(MEETINGS, {}).values():
                p = ppl.get(m.get("person_id") or "")
                if p and not m.get("cancelled") and p.get("stage") in ("mapped", "contacted", "replied"):
                    p["stage"] = "conversation"
    return kept
