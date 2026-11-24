"""A demo: Gus with a made-up search, on its own port, for showing to someone without showing your own.

    python3 -m evals.demo               # http://127.0.0.1:8585, until you press Ctrl-C
    python3 -m evals.demo --port 9000

It's a separate copy of Gus as it is in this folder now, uncommitted changes included, in a temporary folder
(evals/world.py). Nothing of yours is in it: no profile, no data, no keys. Job boards and Claude are the made-up ones in
evals/fake, so it works offline and costs nothing, and Check roles finds two new roles the first time. Each start begins
from the same search, with every date counted back from today. Stopping it deletes the copy.

Every company, person and role here is invented. The repo is public: keep it that way.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from evals.world import SETTINGS, World
from radar.accounts import week_id
from radar.events import slug
from radar.jobs import mark_same_adverts

PORT = 8585
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ago(days: float, hour: int | None = None) -> str:
    """A time this many days back, as Gus stores it."""
    t = NOW - timedelta(days=days)
    return (t.replace(hour=hour, minute=0, second=0) if hour is not None else t).isoformat()


def day(days: float) -> str:
    return str((NOW - timedelta(days=days)).date())


PROFILE = """# Candidate profile

Sam Rivers, a made-up senior product manager in London.

## What I want
- Senior, staff or group product manager roles at AI and developer-tools companies
- London or remote in the UK

## Experience
- Made-up Ltd: Senior Product Manager, platform and developer experience
- Northgate Loop: Product Manager, payments
"""

# name, board slug, fit, status, what it makes, stage, in London, people, investors, why it fits
COMPANIES = [
    ("Harborlight", "harborlight", 9, "watched", "An evaluation and monitoring platform for AI agents", "Series C", "London HQ", 420,
     ["Larkspur Capital"], "Platform product for developers, hiring senior PMs in London."),
    ("Thistledown", "thistledown", 9, "watched", "Tools for testing model quality before release", "Series B", "London office", 180,
     ["Larkspur Capital", "Fieldgate"], "Your evaluation experience is the product."),
    ("Kestrelworks", "kestrelworks", 8, "watched", "Agents that run back-office work for finance teams", "Series B", "London HQ", 240,
     ["Fieldgate"], "Enterprise agents, a small product team with room at staff level."),
    ("Marrowfield", "marrowfield", 8, "watched", "A data platform for clinical trials", "Series C", "London HQ", 600,
     ["Old Quay Partners"], "Regulated data products, close to your platform work."),
    ("Copperline", "copperline", 7, "watched", "Payments risk and ledgers for marketplaces", "Series B", "London office", 310,
     ["Old Quay Partners"], "Payments background from Northgate Loop carries over."),
    ("Saltmarsh Robotics", "saltmarsh", 7, "watched", "Fleet software for warehouse robots", "Series A", "Remote in the UK", 90,
     ["Fieldgate"], "Strong product, but hardware is new ground for you."),
    ("Windlass", "windlass", 7, "watched", "Build and deploy tools for small engineering teams", "Series A", "London office", 70,
     [], "Developer tools at an early stage."),
    ("Quillmere", "quillmere", 7, "watched", "Search for legal documents", "Series B", "London HQ", 150,
     ["Larkspur Capital"], "Search and ranking, a good team, slower hiring."),
    ("Fennimore", "fennimore", 6, "watched", "Scheduling for home-care providers", "Series A", "London HQ", 60, [],
     "A worthwhile product, further from AI."),
    ("Pennywhistle", "pennywhistle", 6, "watched", "Onboarding checklists for HR teams", "Seed", "Remote in the UK", 25, [],
     "Small team: roles are rare and junior."),
    ("Brackenridge", "brackenridge", 5, "watched", "Billing for utilities", "Private", "Reading", 900, [],
     "Stable, but the product roles are delivery-led."),
    ("Oxbow & Finch", "oxbowfinch", 6, "maybe", "Design software for kitchen fitters", "Seed", "London office", 18, [],
     "Interesting founders, too early to say."),
]
FAVOURITES = {"Harborlight", "Thistledown"}

# number on the board, company, title, place, score, why, days since Gus first saw it, the stages you moved it through
# as (stage, days ago), and anything else on the record
ROLES = [
    (101, "Harborlight", "Senior Product Manager, Platform", "London", 9, "Platform PM for developers, at your level and in London.", 24,
     [("shortlisted", 22), ("preparing", 20), ("applied", 17), ("interviewing", 6)], {"note": "Second round with the VP Product on the calendar."}),
    (201, "Kestrelworks", "Staff Product Manager, Agents", "London", 8, "Staff scope on an agents product, with enterprise customers.", 19,
     [("shortlisted", 18), ("preparing", 15), ("applied", 13), ("interviewing", 3)], {}),
    (301, "Thistledown", "Senior Product Manager, Evaluation", "London", 9, "Evaluation tooling is what you've shipped for three years.", 9,
     [("shortlisted", 8), ("preparing", 7), ("applied", 5)], {}),
    (401, "Copperline", "Product Manager, Payments Risk", "London", 8, "Payments risk, where your Northgate Loop years count.", 16,
     [("shortlisted", 15), ("applied", 12)], {"contacts": {"luke-farrow@copperline": ago(11)}}),
    (501, "Marrowfield", "Group Product Manager", "London", 8, "Leads three PMs on a data platform.", 6,
     [("shortlisted", 5), ("preparing", 4), ("applied", 2)], {}),
    (601, "Saltmarsh Robotics", "Senior Product Manager, Fleet", "Remote, UK", 8, "Senior and remote, though robotics is new to you.", 5,
     [("shortlisted", 4), ("preparing", 2)], {"note": "Ask Dev about the simulator roadmap.", "flags": ["Robotics experience preferred", "Salary not stated"]}),
    (701, "Windlass", "Product Lead, Developer Tools", "London", 7, "First product hire: wide scope, small team.", 4,
     [("shortlisted", 3), ("preparing", 1)], {}),
    (302, "Thistledown", "Product Manager, Model Quality", "London", 8, "A level below senior, on the team you'd most like to join.", 3,
     [("shortlisted", 2)], {}),
    (801, "Quillmere", "Senior Product Manager, Search", "London", 7, "Search and ranking at senior level.", 6, [("shortlisted", 4)], {}),
    (502, "Marrowfield", "Senior Product Manager, Growth", "London", 7, "Growth isn't your strongest area, but the company fits.", 2,
     [("shortlisted", 1)], {}),
    (901, "Fennimore", "Senior Product Manager", "London", 7, "Senior generalist role at a small company.", 45,
     [("shortlisted", 44), ("applied", 40), ("closed", 20)], {"note": "They hired internally.", "ad_closed": ago(21)}),
    (951, "Pennywhistle", "Product Manager, Onboarding", "Remote, UK", 7, "Remote, with a small and friendly team.", 38,
     [("shortlisted", 36), ("closed", 30)], {"ad_closed": ago(30)}),
    (102, "Harborlight", "Product Manager, Growth", "London", 8, "A company you favour; growth rather than platform.", 1, [], {}),
    (104, "Harborlight", "Product Operations Manager", "London", 4, "A company you favour, but an operations role.", 2, [], {}),
    (202, "Kestrelworks", "Senior Product Manager, Enterprise", "London", 9, "Senior, enterprise customers, agents: the closest match this week.", 0.4, [], {}),
    (402, "Copperline", "Senior Product Manager, Ledger", "London", 7, "Senior and in payments, but a back-office product.", 2, [], {}),
    (802, "Quillmere", "Product Manager, Trust", "London", 7, "Trust and safety for a search product.", 1, [], {}),
    (702, "Windlass", "Product Manager, Integrations", "London", 6, "A fair match, a level below what you want.", 3, [], {"flags": ["Salary not stated"]}),
    (602, "Saltmarsh Robotics", "Product Owner, Simulation", "Remote, UK", 6, "Delivery-led, in a domain you'd have to learn.", 2, [], {}),
    (971, "Brackenridge", "Senior Product Manager, Data", "Remote, UK", 5, "Senior, but billing for utilities is far from your work.", 4, [], {}),
    (981, "Oxbow & Finch", "Product Manager", "London", 5, "The only product role at a very young company.", 5, [], {}),
    (703, "Windlass", "Senior Product Manager, Hosting", "Remote, UK", 6, "A good role you decided against.", 8,
     [("skipped", 7)], {"skip_reason": "pay"}),
    (403, "Copperline", "Director of Product", "London", 6, "A step above the level you're after.", 10, [("skipped", 9)], {"skip_reason": "senior"}),
    (902, "Fennimore", "Associate Product Manager", "London", 3, "An entry-level role.", 3, [], {"stage": "skipped"}),
    (972, "Brackenridge", "Product Owner, Billing", "Remote, UK", 4, "Delivery-led and outside your field.", 6, [], {"stage": "skipped"}),
    (952, "Pennywhistle", "Junior Product Manager", "Remote, UK", 2, "Junior.", 2, [], {"stage": "skipped"}),
]
# on the boards but not yet in Gus: what Check roles finds the first time it's pressed
NEW_ROLES = [
    (303, "Thistledown", "Senior Product Manager, Safety Tooling", "London", 9, "Senior, on evaluation tooling, at a company you favour."),
    (103, "Harborlight", "Product Manager, Billing", "London", 6, "A company you favour, but billing is a sideways step."),
]

# name, company, their job, kind, known to you, how far along, where they worked before, why Gus thinks they're there
PEOPLE = [
    ("Priya Raman", "Harborlight", "VP Product", "product_leader", False, "mapped", ["Northgate Loop"],
     "Announced her move to Harborlight as VP Product."),
    ("Tom Okafor", "Harborlight", "Senior Product Manager", "peer_pm", True, "mapped", [], "Your connection; lists Harborlight as his employer."),
    ("Elena Marsh", "Kestrelworks", "Head of Product", "hiring_manager", False, "replied", ["Made-up Ltd"],
     "Posted that the agents team is hiring two product managers."),
    ("Jonas Reed", "Kestrelworks", "Talent Partner", "recruiter", False, "mapped", [], "Named as the contact on Kestrelworks' product ads."),
    ("Aisha Bello", "Thistledown", "Director of Product", "hiring_manager", False, "mapped", [], "Listed as the hiring manager on the evaluation role."),
    ("Callum Wright", "Thistledown", "Product Manager", "peer_pm", True, "mapped", ["Made-up Ltd"], "Your connection; works on model quality."),
    ("Marta Nowak", "Copperline", "Group Product Manager", "hiring_manager", False, "mapped", ["Northgate Loop"],
     "Wrote Copperline's post about its payments risk team."),
    ("Luke Farrow", "Copperline", "Recruiter", "recruiter", False, "contacted", [], "Recruits for product at Copperline."),
    ("Dev Patel", "Saltmarsh Robotics", "Chief Product Officer", "product_leader", False, "mapped", [], "Presented the Fleet 2.0 launch."),
    ("Hannah Lind", "Marrowfield", "Senior Product Manager", "announcer", False, "contacted", [], "Posted that her team is hiring a group PM."),
    ("Ravi Menon", "Windlass", "Product Manager", "peer_pm", True, "conversation", [], "Your connection; the only PM at Windlass."),
    ("Sofia Brandt", "Quillmere", "VP Product", "product_leader", False, "contacted", [], "Named as VP Product on Quillmere's team page."),
    ("Grace Osei", "Fennimore", "Product Manager", "peer_pm", False, "mapped", [], "Spoke about Fennimore's scheduling product at a meetup."),
]

# company, type, days ago, what happened
EVENTS = [
    ("Harborlight", "leader_hire", 4, "Priya Raman joined as VP Product, from Northgate Loop."),
    ("Harborlight", "funding", 40, "Raised a £90m Series C led by Larkspur Capital."),
    ("Kestrelworks", "post_hiring", 3, "Elena Marsh posted that the agents team is hiring two product managers."),
    ("Kestrelworks", "launch", 12, "Launched agents for month-end close."),
    ("Thistledown", "funding", 6, "Raised a £60m Series B to grow its evaluation platform."),
    ("Thistledown", "london_office", 20, "Opened a second London office for its product team."),
    ("Copperline", "post_hiring", 2, "Marta Nowak wrote that the payments risk team is doubling this year."),
    ("Saltmarsh Robotics", "launch", 8, "Launched Fleet 2.0, with a simulator for new warehouse layouts."),
    ("Marrowfield", "post_hiring", 5, "Hannah Lind posted that her team is hiring a group product manager."),
    ("Windlass", "leader_hire", 10, "Hired its first VP Engineering."),
    ("Quillmere", "mention", 15, "Named in a round-up of legal technology companies to watch."),
    ("Quillmere", "funding", 55, "Raised a £25m Series B."),
    ("Fennimore", "launch", 25, "Launched a mobile app for care workers."),
]

# person, company, the event's type, status, outcome, weeks ago, why now, angle, draft and, where the product is free to get into, what to try
MOVES = [
    ("Priya Raman", "Harborlight", "leader_hire", "todo", None, 0, "She started four days ago and is building her team.",
     "You both worked at Northgate Loop, a few years apart.",
     "Hi Priya, congratulations on the move to Harborlight. I spent four years on payments at Northgate Loop, a little after "
     "your time there, and I've been building evaluation tools since. I'd like to hear what you plan to change first."),
    ("Marta Nowak", "Copperline", "post_hiring", "todo", None, 0, "Her post about the risk team went up two days ago.",
     "Your payments risk work at Northgate Loop.",
     "Hi Marta, I read your post about doubling the payments risk team. I ran risk tooling for marketplaces at Northgate Loop and "
     "have applied for the Payments Risk role. Could I ask you two questions about how the team is set up?"),
    ("Dev Patel", "Saltmarsh Robotics", "launch", "todo", None, 0, "Fleet 2.0 launched last week.",
     "A question about the simulator, from someone who builds test tools.",
     "Hi Dev, the Fleet 2.0 simulator caught my eye: I build tools for testing software before release. How do you decide a "
     "new layout is safe to ship? I'm preparing an application for the Fleet role and would value ten minutes.",
     "The Fleet 2.0 simulator has a free trial. Upload a floor plan and run one layout change through it."),
    ("Elena Marsh", "Kestrelworks", "post_hiring", "done", "replied", 0, "She posted about hiring three days ago.",
     "You both worked at Made-up Ltd.",
     "Hi Elena, I saw your post about the agents team. We overlapped at Made-up Ltd, and I've applied for the Staff role. "
     "I'd welcome a short conversation about what the first six months look like."),
    ("Hannah Lind", "Marrowfield", "post_hiring", "done", None, 0, "She posted about the group PM role five days ago.",
     "Ask what the team needs from its next lead.",
     "Hi Hannah, I saw your post about the group product manager role. I've applied, and I'd like to hear from someone on "
     "the team what you most need from the person who takes it."),
    ("Ravi Menon", "Windlass", "leader_hire", "done", "conversation", 1, "Windlass had just hired its first VP Engineering.",
     "You know each other already.",
     "Hi Ravi, I saw Windlass has a VP Engineering now. I'm looking at the Product Lead role: could we catch up this week?"),
    ("Sofia Brandt", "Quillmere", "mention", "done", "no_reply", 1, "Quillmere was in a round-up of companies to watch.",
     "Search ranking, which you've worked on.",
     "Hi Sofia, congratulations on the write-up. I've worked on search ranking and shortlisted your Senior PM role. "
     "Would you be open to a short call?"),
]

# source, kind, title, days ago, status, the company it turned out to be about
SIGNALS = [
    ("Made-up Product Weekly", "newsletter", "Kestrelworks ships agents for month-end close", 12, "processed", "Kestrelworks"),
    ("Made-up Funding Wire", "signals", "Thistledown raises £60m Series B", 6, "processed", "Thistledown"),
    ("Made-up Robotics Digest", "signals", "Saltmarsh Robotics launches Fleet 2.0", 8, "processed", "Saltmarsh Robotics"),
    ("Made-up Funding Wire", "signals", "Lanternfield raises a seed round for agent testing", 1, "new", None),
    ("Made-up Product Weekly", "newsletter", "Five London teams hiring product managers this month", 0.5, "new", None),
    ("Made-up Jobs Board", "jobs", "Head of Product at a stealth fintech", 2, "new", None),
    ("Made-up Community", "community", "What do staff PMs do all day?", 9, "ignored", None),
    ("Made-up Funding Wire", "signals", "A US-only company raises a Series D", 14, "ignored", None),
]

# name, company, position, connected on: a made-up LinkedIn export
CONNECTIONS = [
    ("Tom Okafor", "Harborlight", "Senior Product Manager", "12 Mar 2021"),
    ("Callum Wright", "Thistledown", "Product Manager", "03 Sep 2022"),
    ("Ravi Menon", "Windlass", "Product Manager", "21 Jan 2020"),
    ("Nina Castell", "Northgate Loop", "Engineering Manager", "08 Jun 2019"),
    ("Owen Platt", "Larkspur Capital", "Partner", "30 Nov 2023"),
    ("Beth Hargreave", "Made-up Ltd", "Design Lead", "14 Feb 2022"),
    ("Idris Kane", "Kestrelworks", "Solutions Engineer", "19 Jul 2024"),
    ("Mei Tanaka", "Copperline", "Data Scientist", "02 May 2023"),
]

# the news feeds the demo reads: both answer, with nothing new
FEEDS = [("Made-up Funding Wire", "https://example.com/feeds/funding.xml", "signals"),
         ("Made-up Jobs Board", "https://example.com/feeds/jobs.xml", "jobs")]

MEETINGS = [
    ("Harborlight", 2, 14, "Interview: Senior Product Manager, Platform", "Second round, with the VP Product", "Harborlight recruiting"),
    ("Kestrelworks", 5, 10, "Interview: Staff Product Manager, Agents", "Product case study", "Jonas Reed"),
]


def link(*parts) -> str:
    return "https://example.com/" + "/".join(slug(str(p)) for p in parts)


def board_slug(company) -> str:
    return next(c[1] for c in COMPANIES if c[0] == company)


def role_id(n, company) -> str:
    return f"gh:{board_slug(company)}:{n}"


SHARED_ADVERT = {"Senior Product Manager, Platform", "Product Manager, Growth"}  # one advert under two titles, so each says "Same advert as"


def advert(company, title, place) -> str:
    made = next(c[4] for c in COMPANIES if c[0] == company)
    if title in SHARED_ADVERT:
        return (f"{company} makes {made[0].lower()}{made[1:]}. We're hiring product managers in {place}. You'll own the roadmap for your "
                "area, work with engineering and design every day, talk to customers every week and decide what we build next. "
                "You'll set the measures that tell us whether a launch worked, write the plan the team builds from and say no to "
                "the things that don't matter yet. We work from the office three days a week and welcome applications from "
                "every background. This is a made-up advert for the Gus demo, used for more than one role.")
    return (f"{company} makes {made[0].lower()}{made[1:]}. We're hiring a {title} in {place}. You'll own the roadmap for your area, "
            "work with engineering and design every day, talk to customers every week and decide what we build next. "
            "This is a made-up advert for the Gus demo.")


def companies() -> dict:
    out = {name: {"status": status, "fit": fit, "fit_why": why, "assessed": day(9), "source": "watchlist agent", "product": product,
                  "stage": stage, "london": london, "headcount": people, "investors": investors, "board": f"greenhouse:{board}",
                  "website": f"https://{board}.example", "website_checked": ago(2), "favourite": name in FAVOURITES, "added": day(60)}
           for name, board, fit, status, product, stage, london, people, investors, why in COMPANIES}
    out["Lanternfield"] = {"status": "candidate", "added": day(1), "source": "discover agent", "signal": "Raised a seed round for agent testing",
                           "board_guess": {"board": "greenhouse:lanternfield", "url": "https://job-boards.greenhouse.io/lanternfield",
                                           "open_roles": 3, "found": day(1)}}
    return out


def jobs() -> dict:
    out = {}
    for n, company, title, place, score, why, seen, moved, extra in ROLES:
        jid = role_id(n, company)
        out[jid] = {"id": jid, "company": company, "title": title, "location": place, "url": link("roles", company, n), "posted": ago(seen + 1),
                    "description": advert(company, title, place), "text_kind": "posting", "source": "board", "score": score, "why": why,
                    "flags": [], "first_seen": ago(seen), "last_seen": ago(0.1), "stage": moved[-1][0] if moved else "new",
                    "history": [{"stage": s, "at": ago(d, 10)} for s, d in moved], **extra}
    mark_same_adverts(out)
    return out


def changes() -> list:
    """The last week of the change log: what you moved, and the roles Gus skipped for their score."""
    out = []
    for n, company, *_, score, _why, seen, moved, extra in ROLES:
        steps = [(old, new, d, "you", None) for (old, _), (new, d) in zip([("new", 0)] + moved, moved)]
        if extra.get("stage") == "skipped":
            steps.append(("new", "skipped", seen, "triage", f"scored {score}/10"))
        for old, new, d, actor, reason in steps:
            if d <= 7:
                at = ago(d, 10)
                out.append({"id": f"chg_{at[:19].replace('-', '').replace(':', '')}_{n:04x}", "at": at, "role": role_id(n, company),
                            "field": "stage", "old": old, "new": new, "actor": actor, **({"reason": reason} if reason else {})})
    return sorted(out, key=lambda c: c["at"])


def person_id(name, company) -> str:
    return f"{slug(name)}@{slug(company)}"


def people() -> dict:
    return {person_id(name, company): {
        "name": name, "company": company, "role": role, "kind": kind, "platform": "linkedin", "url": link("people", name),
        "evidence": evidence, "evidence_date": day(6), "confidence": "high", "past_companies": past, "stage": stage, "added": day(14),
        "source": "people search", **({"warm": True, "warm_how": "1st-degree LinkedIn connection"} if warm else {})}
        for name, company, role, kind, warm, stage, past, evidence in PEOPLE}


def event_id(company, kind) -> str:
    return next(f"{slug(c)}:{k}:{day(d)}" for c, k, d, _ in EVENTS if (c, k) == (company, kind))


def events() -> dict:
    return {f"{slug(company)}:{kind}:{day(d)}": {"account": company, "type": kind, "summary": summary, "url": link("news", company, kind),
                                                  "date": day(d), "source": "agent", "added": day(d)}
            for company, kind, d, summary in EVENTS}


def moves() -> dict:
    out = {}
    for name, company, kind, status, outcome, weeks, why_now, angle, draft, *teardown in MOVES:
        made = NOW - timedelta(weeks=weeks)
        pid = person_id(name, company)
        mid = f"{week_id(made.date())}:{pid}"
        out[mid] = {"id": mid, "week": week_id(made.date()), "account": company, "person_id": pid, "event_id": event_id(company, kind),
                    "why_now": why_now, "angle": angle, "draft": draft, "channel": "linkedin", "give_first": "", "status": status,
                    "outcome": outcome, "created": (made - timedelta(hours=30)).isoformat()}
        if teardown:
            out[mid]["teardown"] = {"try": teardown[0], "url": "https://example.com/" + slug(company) + "/sign-up"}
        if status == "done":
            out[mid]["done_at"] = (made - timedelta(hours=20)).isoformat()
        if outcome:
            out[mid]["outcome_at"] = (made - timedelta(hours=3)).isoformat()
    return out


def signals() -> dict:
    return {f"demo:{n}": {"source": source, "kind": kind, "title": title, "text": title + ". A made-up item for the Gus demo.",
                          "url": link("signals", n), "date": day(d), "status": status, "first_seen": ago(d),
                          **({"company": company} if company else {})}
            for n, (source, kind, title, d, status, company) in enumerate(SIGNALS, 1)}


def network() -> dict:
    return {"imported": day(30), "connections": [{"name": name, "url": link("people", name), "company": company, "position": position,
                                                  "connected_on": on} for name, company, position, on in CONNECTIONS]}


def meetings() -> dict:
    return {f"demo-{n}": {"start": (NOW + timedelta(days=d)).replace(hour=hour, minute=0, second=0).isoformat(), "title": title, "account": company,
                          "why": why, "organizer": who, "location": link("meet", company)}
            for n, (company, d, hour, title, why, who) in enumerate(MEETINGS, 1)}


def usage() -> list:
    """Two weeks of what the AI cost: a scoring run most days, research twice a week."""
    rows = []
    for d in range(13, -1, -1):
        at = (datetime.now() - timedelta(days=d)).replace(hour=7, minute=5, second=0, microsecond=0)
        rows.append({"kind": "ai", "task": "scoring", "provider": "claude_subscription", "model": "claude-sonnet-5-5", "cost_usd": 0.04 + 0.01 * (d % 3),
                     "searches": 0, "input_tokens": 9000, "output_tokens": 1200, "at": at.isoformat()})
        if d % 4 == 1:
            rows.append({"kind": "ai", "task": "agent", "provider": "claude_subscription", "model": "claude-sonnet-5-5", "cost_usd": 0.62,
                         "searches": 14, "input_tokens": 80000, "output_tokens": 6000, "at": at.replace(minute=20).isoformat()})
        if d % 5 == 2:
            rows.append({"kind": "ai", "task": "people", "provider": "claude_subscription", "model": "claude-sonnet-5-5", "cost_usd": 0.18,
                         "searches": 6, "input_tokens": 30000, "output_tokens": 2500, "at": at.replace(minute=40).isoformat()})
    return rows


def boards() -> dict:
    """What the made-up internet answers (evals/fake/sitecustomize.py): each company's board, with the roles Gus has
    and two it hasn't found yet, and each company's own site."""
    pages = {}
    live = [r[:4] for r in ROLES if "ad_closed" not in r[8]] + [r[:4] for r in NEW_ROLES]
    for name, board, *_ in COMPANIES:
        base = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
        listed = [{"id": n, "title": title, "absolute_url": link("roles", company, n), "location": {"name": place},
                   "first_published": ago(1)} for n, company, title, place in live if company == name]
        pages[base] = {"jobs": listed}
        for row in listed:
            pages[f"{base}/{row['id']}"] = {**row, "content": f"<p>{advert(name, row['title'], row['location']['name'])}</p>", "offices": []}
        pages[f"https://{board}.example/"] = (f"<html><head><title>{name}</title></head><body>"
                                              f'<a href="https://job-boards.greenhouse.io/{board}">Careers</a></body></html>')
    for _, url, _ in FEEDS:
        pages[url] = '<?xml version="1.0"?><rss version="2.0"><channel><title>Made-up feed</title></channel></rss>'
    return pages


def scores() -> dict:
    """What the made-up Claude says about each role (evals/fake/claude), by title."""
    return {title: [score, why] for _, _, title, _, score, why, *_ in ROLES + NEW_ROLES}


def fill(world: World) -> None:
    """Replace a made-up install's data with the demo search."""
    app, data = world.app, world.app / "data"
    data.mkdir(exist_ok=True)
    (app / "profile" / "profile.md").write_text(PROFILE)
    (app / "watchlist.csv").write_text("company,ats,slug,sector\n" + "".join(f"{c[0]},greenhouse,{c[1]},software\n" for c in COMPANIES))
    (app / "settings.json").write_text(json.dumps({
        **SETTINGS, "title_include": "product manager\nproduct owner\nproduct lead\ndirector of product",
        "my_employers": "Made-up Ltd\nNorthgate Loop", "source_hn": False, "source_mastodon": False, "source_bluesky": False, "source_x": False,
        "feeds_extra": "\n".join(f"{name} | {url} | {kind} | 30" for name, url, kind in FEEDS)}))
    for name, made in (("companies", companies), ("jobs", jobs), ("people", people), ("events", events), ("moves", moves),
                       ("signals", signals), ("network", network), ("meetings", meetings)):
        (data / f"{name}.json").write_text(json.dumps(made(), indent=1, ensure_ascii=False))
    (data / "changes.jsonl").write_text("".join(json.dumps(c) + "\n" for c in changes()))
    (data / "usage.jsonl").write_text("".join(json.dumps(r) + "\n" for r in usage()))
    (data / "board_health.json").write_text(json.dumps({c[0]: {"board": f"greenhouse:{c[1]}", "read": ago(0.1)} for c in COMPANIES}))
    (data / "board_history.json").write_text(json.dumps({ago(d)[:10]: {"boards": len(COMPANIES), "unread": [c[0] for c in COMPANIES[:1 if d in (4, 5) else 0]],
                                                                       "no_board": 2, "no_board_watched": 1} for d in range(20, -1, -1)}))
    (data / "heartbeat.log").write_text(f"{ago(1.1)} ok\n{ago(0.1)} ok\n")
    (data / "runs").mkdir(exist_ok=True)  # yesterday's full run, so Activity has a last run to show
    (data / "runs" / f"discover-{ago(1, 7)[:19].replace(':', '-')}Z.md").write_text("# Discover\n\nA made-up run for the Gus demo.\n")
    (data / "demo").write_text("Made-up data: the cockpit says so in its menu.\n")
    (world.home / "boards.json").write_text(json.dumps(boards()))
    (world.home / "scores.json").write_text(json.dumps(scores()))
    done = subprocess.run([sys.executable, "-m", "radar.events", "sync"], cwd=app, env=world.env, capture_output=True, text=True)
    if done.returncode:  # roles scoring 7+ become events at their company, as after any scan
        raise RuntimeError(f"The demo's events didn't sync:\n{done.stderr[-600:]}")


def build() -> World:
    world = World()
    try:
        fill(world)
    except BaseException:
        world.close()
        raise
    return world


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # stopped from the browser pane: still delete the copy
    world = build()
    try:
        world.start(port)
        print(f"Gus demo on {world.url}  (made-up data; Ctrl-C to stop)", flush=True)
        world.server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        world.close()


if __name__ == "__main__":
    main()
