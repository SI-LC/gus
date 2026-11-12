"""python3 -m unittest discover -s tests"""
import unittest
from datetime import datetime, timezone

import email
import re

from radar import agent, ai, calendar_invites as cal, inbox
from sample_search import use_sample
from radar.network import distance, paths_for
from radar.service import due, sources_due

def at(day, h, m=0):
    return datetime(2026, 11, day, h, m)


class RemoteRoles(unittest.TestCase):
    def setUp(self):
        use_sample(self)

    def job(self, location):
        return {"title": "Product Manager", "location": location, "description": ""}

    def test_a_repeated_remote_location_counts_as_remote(self):
        from radar import matching
        cfg = matching.load_config()
        self.assertTrue(matching.matches(self.job("Remote / Remote "), cfg))
        self.assertTrue(matching.matches(self.job("Remote"), cfg))

    def test_remote_in_one_country_still_does_not_count(self):
        from radar import matching
        cfg = matching.load_config()
        self.assertFalse(matching.matches(self.job("Remote / United States"), cfg))


class Feeds(unittest.TestCase):
    RSS = b"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
      <title>Who is hiring</title><link>https://list.example/1</link><description>Short teaser</description>
      <content:encoded><![CDATA[<p>Acme is hiring a <a href="https://acme.example/pm">product manager</a> in London.</p>]]></content:encoded>
    </item></channel></rss>"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from radar import feeds
        self.feeds = feeds
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.rows = [{"name": "Good", "url": "https://good.example", "kind": "jobs", "enabled": True},
                     {"name": "Bad", "url": "https://bad.example", "kind": "signals", "enabled": True}]
        for patcher in (mock.patch.object(feeds, "DATA", self.data), mock.patch.object(feeds, "SIGNALS", self.data / "signals.json"),
                        mock.patch.object(feeds, "all_feeds", lambda: self.rows), mock.patch.object(feeds, "fetch_bytes", self.answer)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.broken = True

    def answer(self, url, timeout=30):
        if self.broken and "bad" in url:
            raise OSError("HTTP Error 403: Forbidden")
        return self.RSS

    def errors(self):
        import json
        return json.loads((self.data / "feed_errors.json").read_text())

    def test_a_job_feed_keeps_the_full_post_and_its_links(self):
        item = next(self.feeds.fetch("https://good.example", full=True))
        self.assertIn("product manager (https://acme.example/pm)", item["text"])

    def test_other_feeds_keep_the_teaser(self):
        self.assertEqual(next(self.feeds.fetch("https://good.example"))["text"], "Short teaser")

    def test_a_job_feed_without_full_text_falls_back_to_the_teaser(self):
        self.RSS = self.RSS.replace(b"content:encoded", b"content:other")
        self.assertEqual(next(self.feeds.fetch("https://good.example", full=True))["text"], "Short teaser")

    def test_a_failing_feed_is_reported_then_cleared_when_it_works_again(self):
        import json
        self.feeds.main()
        self.assertEqual(self.errors(), {"Bad": "HTTP Error 403: Forbidden"})
        self.broken = False
        state = self.data / "feeds_state.json"
        state.write_text(json.dumps({"Good": 0}))
        self.feeds.main()
        self.assertEqual(self.errors(), {})

    def test_a_turned_off_feed_is_no_longer_reported(self):
        self.feeds.main()
        self.rows[1]["enabled"] = False
        self.feeds.main()
        self.assertEqual(self.errors(), {})


class AppIcon(unittest.TestCase):
    def test_browser_icon_has_light_and_dark_versions(self):
        from radar.paths import ROOT
        self.assertIn("prefers-color-scheme:dark", (ROOT / "radar" / "static" / "icon.svg").read_text())

    def test_every_icon_file_exists(self):
        import json
        from radar import server
        for name, _ in server.STATIC.values():
            self.assertTrue((server.ROOT / "radar" / "static" / name).exists(), name)
        for icon in json.loads(server.MANIFEST)["icons"]:
            self.assertIn(icon["src"], server.STATIC)


class CockpitStyles(unittest.TestCase):
    """The stylesheet Gus serves is built by Tailwind from radar/cockpit.css and the classes the page uses."""

    def test_the_built_stylesheet_matches_its_source(self):
        from radar import css
        self.assertEqual(css.built_from(), css.stamp(),
                         "radar/cockpit.css or a class in radar/cockpit.html changed: rebuild with python3 -m radar.css")

    def test_the_page_loads_the_stylesheet_gus_serves(self):
        from radar import server
        self.assertIn('<link rel="stylesheet" href="/cockpit.css">', server.PAGE.read_text())
        self.assertIn("/cockpit.css", server.STATIC)
        self.assertIn("style-src 'self'", server.CSP)

    def test_a_change_to_the_pages_script_needs_no_rebuild(self):
        from unittest import mock
        from radar import css
        before = css.stamp()
        page = css.PAGE.read_text()
        with mock.patch.object(css, "PAGE", mock.Mock(read_text=lambda: page.replace("let S =", "let  S ="))):
            self.assertEqual(css.stamp(), before)
        with mock.patch.object(css, "PAGE", mock.Mock(read_text=lambda: page.replace('class="toast"', 'class="toast mt-8"'))):
            self.assertNotEqual(css.stamp(), before)


class Schedule(unittest.TestCase):
    CFG = {"run_time": "08:00"}

    def test_job_sources_check_daily_with_the_daily_run_off(self):
        cfg = {"scheduler_enabled": False, "sources_auto": True, "sources_run_time": "09:00", "source_adzuna": True}
        self.assertFalse(sources_due(at(4, 8, 59), cfg, {}))
        self.assertTrue(sources_due(at(4, 9), cfg, {}))
        self.assertFalse(sources_due(at(4, 10), cfg, {"sources": str(at(4, 10).date())}))
        self.assertFalse(sources_due(at(4, 9), {**cfg, "sources_auto": False}, {}))
        self.assertFalse(sources_due(at(4, 9), {**cfg, "source_adzuna": False}, {}), "nothing automatic with sources off")


    def test_nothing_before_the_daily_time(self):
        self.assertFalse(due(at(4, 7, 59), self.CFG, {}))

    def test_runs_once_a_day(self):
        self.assertTrue(due(at(4, 8), self.CFG, {}))
        self.assertFalse(due(at(4, 13), self.CFG, {"run": "2026-11-04"}))
        self.assertTrue(due(at(5, 8), self.CFG, {"run": "2026-11-04"}))

    def test_empty_time_means_button_only(self):
        self.assertFalse(due(at(4, 12), {"run_time": ""}, {}))


class FullRun(unittest.TestCase):
    def run_with(self, *, emails=0, moves=0, watchlist_due=True, fail=None, budget_at=None):
        from unittest import mock
        from radar import ai, service
        calls = []

        def step(name):
            def go():
                if name == budget_at:
                    raise ai.BudgetReached("limit reached")
                if name == fail:
                    raise RuntimeError("boom")
                calls.append(name)
                return True, ""
            return go
        with mock.patch.multiple(service, _check_roles=step("scan"), _agent_step=step, _boards=step("boards"), _run_modules=lambda *a: step("score")(),
                                  _emails_waiting=lambda: emails, _moves_this_week=lambda: moves,
                                  _watchlist_due=lambda c, s: watchlist_due, _counts=lambda: {"roles": 0, "strong": 0},
                                  _schedule_state=lambda: {}, _save_schedule_state=lambda s: None, load_config=lambda: {}):
            service.progress.clear()
            ok, summary = service.full_run()
        return ok, summary, calls, {s["name"]: s["status"] for s in service.progress["steps"]}

    def test_runs_in_order(self):
        _, _, calls, _ = self.run_with(emails=1)
        self.assertEqual(calls, ["scan", "inbox", "discover", "boards", "watchlist", "score", "moves"])

    def test_skips_what_it_doesnt_need(self):
        _, _, calls, steps = self.run_with(moves=5, watchlist_due=False)
        self.assertEqual(calls, ["scan", "discover", "boards", "score"])
        self.assertEqual(steps["Plan moves"], "skipped")

    def test_one_failure_doesnt_lose_the_rest(self):
        ok, _, calls, steps = self.run_with(fail="discover")
        self.assertFalse(ok)
        self.assertEqual(steps["Discover"], "failed")
        self.assertIn("moves", calls)

    def test_ai_limit_stops_ai_steps(self):
        ok, summary, calls, steps = self.run_with(budget_at="discover")
        self.assertFalse(ok)
        self.assertIn("limit reached", summary)
        self.assertEqual(calls, ["scan"])
        self.assertEqual(steps["Plan moves"], "skipped")


class RunProgress(unittest.TestCase):
    """While a scan runs, the cockpit can say what it's doing, and when it ends, what it found."""

    def check(self, returncode, counts):
        import subprocess
        from unittest import mock
        from radar import service
        done = subprocess.CompletedProcess([], returncode, stdout="", stderr="board timed out")
        with mock.patch.object(service.subprocess, "run", return_value=done), \
             mock.patch.object(service, "_counts", side_effect=counts):
            return service._check_roles()

    def test_check_roles_says_what_it_found(self):
        ok, summary = self.check(0, [{"roles": 10, "strong": 1}, {"roles": 13, "strong": 2}])
        self.assertTrue(ok)
        self.assertEqual(summary, "3 new roles, 2 strong in your inbox")

    def test_check_roles_says_when_it_failed(self):
        ok, summary = self.check(1, [{"roles": 10, "strong": 1}])
        self.assertFalse(ok)
        self.assertIn("Didn't finish", summary)
        self.assertIn("board timed out", summary)

    def test_status_carries_what_is_happening_now(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from radar import doing, service
        with tempfile.TemporaryDirectory() as d, mock.patch.object(doing, "DATA", Path(d)), \
                mock.patch.object(doing, "NOW", Path(d) / "doing.txt"), mock.patch.dict(service.running, {"scan": "2026-11-12T10:00:00"}):
            doing.say("Reading job boards: 3 of 9 done")
            self.assertEqual(service.status()["progress"]["now"], "Reading job boards: 3 of 9 done")
            doing.clear()
            self.assertEqual(service.status()["progress"]["now"], "")


class Budget(unittest.TestCase):
    def test_zero_blocks_every_run(self):
        from unittest import mock
        with mock.patch("radar.notify.notify"):  # a test must never send a real notification
            with self.assertRaises(ai.BudgetReached):
                ai.check_budget({"ai_daily_budget_usd": 0})
            with self.assertRaises(ai.BudgetReached):
                ai.check_budget({})

    def test_room_left_allows_a_run(self):
        ai.check_budget({"ai_daily_budget_usd": 1000000})


class Inbox(unittest.TestCase):
    SENDERS = ["linkedin.com", "ashbyhq.com"]
    SUBJECTS = re.compile("interview|next steps", re.I)

    def test_only_matching_emails_are_opened(self):
        self.assertTrue(inbox.matches("LinkedIn <messages-noreply@linkedin.com>", "New message", self.SENDERS, self.SUBJECTS))
        self.assertTrue(inbox.matches("Sam <sam@acme.com>", "Interview on Friday", self.SENDERS, self.SUBJECTS))
        self.assertFalse(inbox.matches("Mum <mum@example.com>", "Sunday lunch", self.SENDERS, self.SUBJECTS))

    def test_sender_name_alone_does_not_match(self):
        self.assertFalse(inbox.matches('"linkedin.com" <spam@example.com>', "Hello", self.SENDERS, None))

    def test_body_prefers_plain_text(self):
        msg = email.message_from_string("Content-Type: text/plain; charset=utf-8\n\nHello   there\nworld")
        self.assertEqual(inbox.body_text(msg), "Hello there world")

    def test_html_body_is_stripped(self):
        msg = email.message_from_string("Content-Type: text/html; charset=utf-8\n\n<p>Role <b>open</b></p>")
        self.assertEqual(inbox.body_text(msg), "Role open")


ICS = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
UID:abc@google.com
DTSTART:20261110T100000Z
DTEND:20261110T103000Z
SUMMARY:Intro chat\\, Acme and
  Pat
ORGANIZER;CN=Sam Lee:mailto:sam@acme.io
ATTENDEE;CN=Pat Doe:mailto:pat@example.com
LOCATION:https://meet.google.com/abc
END:VEVENT
END:VCALENDAR
"""


class Calendar(unittest.TestCase):
    def test_parses_a_google_invite(self):
        ev = cal.parse_ics(ICS)[0]
        self.assertEqual(ev["title"], "Intro chat, Acme and Pat")
        self.assertEqual(ev["start"], "2026-11-10T10:00:00+00:00")
        self.assertEqual(ev["organizer"], {"name": "Sam Lee", "email": "sam@acme.io"})

    def test_cancelled(self):
        self.assertTrue(cal.parse_ics(ICS.replace("METHOD:REQUEST", "METHOD:CANCEL"))[0]["cancelled"])

    def test_personal_meetings_are_dropped(self):
        ev = {"title": "Dentist", "organizer": {"name": "Smile Clinic", "email": "hi@smile.co.uk"}, "attendees": []}
        self.assertIsNone(cal.relevance(ev, {"Acme": {"status": "watched"}}, {}))

    def test_tracked_account_is_kept(self):
        ev = {"title": "Catch up", "organizer": {"name": "Sam", "email": "sam@acme.io"}, "attendees": []}
        self.assertEqual(cal.relevance(ev, {"Acme": {"status": "watched"}}, {})["account"], "Acme")

    def test_mapped_person_is_kept(self):
        ev = {"title": "Catch up", "organizer": {"name": "Sam Lee", "email": "sam@gmail.com"}, "attendees": []}
        self.assertEqual(cal.relevance(ev, {}, {"p": {"name": "Sam Lee", "company": "Acme"}})["person_id"], "p")

    def test_interview_is_kept(self):
        ev = {"title": "Interview with the team", "attendees": []}
        self.assertIsNotNone(cal.relevance(ev, {}, {}))


class AgentLimits(unittest.TestCase):
    def test_email_agent_has_no_internet(self):
        self.assertFalse({"WebSearch", "WebFetch"} & set(agent.OFFLINE))

    def test_web_agents_cant_read_email(self):
        self.assertIn("Read(./data/emails.json)", agent.NO_EMAIL)


class RoleFolder(unittest.TestCase):
    def save(self, folder):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from radar import server
        f = Path(tempfile.mkdtemp()) / "jobs.json"
        f.write_text(json.dumps({"j": {"id": "j"}}))
        with mock.patch.object(server, "JOBS", f):
            return server.update_job({"id": "j", "folder": folder})

    def test_web_links_and_paths_are_kept(self):
        for good in ("https://drive.google.com/drive/folders/abc", "http://example.com/x", "~/Documents/Acme/"):
            self.assertEqual(self.save(good)["job"]["folder"], good)

    def test_other_schemes_are_refused(self):
        for bad in ("javascript://alert(1)", "file:///etc/passwd"):
            self.assertIn("error", self.save(bad))


class ClosedAds(unittest.TestCase):
    NOW = "2026-11-05T08:00:00+00:00"

    def scan(self, store, answered=frozenset({"ashby:acme"}), live=frozenset()):
        from radar.scan import mark_closed
        return mark_closed(store, set(answered), set(live), self.NOW)

    def test_gone_for_two_scans_closes(self):
        store = {"ashby:acme:1": {"title": "PM"}}
        self.assertEqual(self.scan(store), [])
        self.assertNotIn("ad_closed", store["ashby:acme:1"])
        self.assertEqual(len(self.scan(store)), 1)
        self.assertEqual(store["ashby:acme:1"]["ad_closed"], self.NOW)

    def test_a_blip_doesnt_close(self):
        store = {"ashby:acme:1": {}}
        self.scan(store)
        self.scan(store, live={"ashby:acme:1"})
        self.scan(store)
        self.assertNotIn("ad_closed", store["ashby:acme:1"])

    def test_a_board_that_failed_closes_nothing(self):
        store = {"ashby:acme:1": {}}
        for _ in range(3):
            self.scan(store, answered=set())
        self.assertNotIn("ad_closed", store["ashby:acme:1"])

    def test_a_returning_ad_reopens(self):
        store = {"ashby:acme:1": {"ad_closed": "x", "missed": 2}}
        self.scan(store, live={"ashby:acme:1"})
        self.assertEqual(store["ashby:acme:1"], {})

    def test_stage_is_left_alone(self):
        store = {"ashby:acme:1": {"stage": "applied"}}
        self.scan(store); self.scan(store)
        self.assertEqual(store["ashby:acme:1"]["stage"], "applied")

    def test_roles_not_from_a_scanned_board_are_left_alone(self):
        store = {"hn:1": {}, "manual:x": {}, "sr:wise:1": {}}
        self.scan(store); self.scan(store)
        self.assertTrue(all("ad_closed" not in j for j in store.values()))

    def test_closed_roles_stop_being_events(self):
        from radar import events as E
        stored = {"role:j": {"account": "Acme", "type": "role_posted", "url": "u", "date": "2026-11-01", "closed": "x"},
                  "role:k": {"account": "Acme", "type": "role_posted", "url": "v", "date": "2026-11-01"}}
        self.assertEqual([e["id"] for e in E.for_account("Acme", stored)], ["role:k"])


class FollowUps(unittest.TestCase):
    NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    JOB = {"id": "j", "company": "Acme", "title": "PM", "stage": "applied",
           "history": [{"stage": "applied", "at": "2026-11-04T10:00:00+00:00"}]}

    def rows(self, job=None, people=None, moves=None):
        from radar.server import follow_ups
        people = {"p": {"name": "Pat", "company": "Acme", "kind": "hiring_manager", "stage": "mapped"}} if people is None else people
        return follow_ups({"j": job or self.JOB}, people, moves or {}, self.NOW)

    def test_applied_role_lists_people_and_why(self):
        r = self.rows()[0]
        self.assertEqual((r["days"], r["contacts"][0]["name"]), (1, "Pat"))
        self.assertIn("hiring manager", r["contacts"][0]["reason"])

    def test_role_with_nobody_mapped_still_shows(self):
        self.assertEqual(self.rows(people={})[0]["contacts"], [])

    def test_only_applied_roles(self):
        self.assertEqual(self.rows(job={**self.JOB, "stage": "preparing"}), [])

    def test_contacted_people_drop_off(self):
        self.assertEqual(self.rows(job={**self.JOB, "contacts": {"p": "2026-11-04"}}), [])
        self.assertEqual(self.rows(people={"p": {"name": "Pat", "company": "Acme", "stage": "contacted"}}, job={**self.JOB, "contacts": {"p": "x"}}), [])

    def test_person_with_a_planned_move_isnt_listed_twice(self):
        self.assertEqual(self.rows(moves={"m": {"person_id": "p", "status": "todo"}}), [])

    def test_contacted_through_a_move_drops_off(self):
        people = {"p": {"name": "Pat", "company": "Acme", "stage": "contacted"}}
        self.assertEqual(self.rows(people=people, moves={"m": {"person_id": "p", "status": "done"}}), [])

    def test_old_applications_drop_off(self):
        old = {**self.JOB, "history": [{"stage": "applied", "at": "2026-09-01T10:00:00+00:00"}]}
        self.assertEqual(self.rows(job=old), [])


class RoleContact(unittest.TestCase):
    def test_logging_a_message_updates_role_move_and_person(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from radar import accounts as A, server
        d = Path(tempfile.mkdtemp())
        files = {n: d / f"{n}.json" for n in ("jobs", "moves", "people")}
        files["jobs"].write_text(json.dumps({"j": {"id": "j"}}))
        files["moves"].write_text(json.dumps({"m": {"person_id": "p", "status": "todo"}, "other": {"person_id": "q", "status": "todo"}}))
        files["people"].write_text(json.dumps({"p": {"name": "Pat", "stage": "mapped"}}))
        with mock.patch.object(server, "JOBS", files["jobs"]), mock.patch.object(A, "MOVES", files["moves"]), mock.patch.object(A, "PEOPLE", files["people"]):
            self.assertTrue(server.role_contact({"id": "j", "person_id": "p"}).get("ok"))
            moves, people, jobs = [json.loads(files[n].read_text()) for n in ("moves", "people", "jobs")]
            self.assertEqual((moves["m"]["status"], moves["other"]["status"]), ("done", "todo"))
            self.assertEqual(people["p"]["stage"], "contacted")
            self.assertIn("p", jobs["j"]["contacts"])
            server.role_contact({"id": "j", "person_id": "p", "sent": False})
            moves, people, jobs = [json.loads(files[n].read_text()) for n in ("moves", "people", "jobs")]
            self.assertEqual((moves["m"]["status"], people["p"]["stage"], jobs["j"]["contacts"]), ("todo", "mapped", {}))


class PeopleSearchReply(unittest.TestCase):
    def test_reads_the_list_when_more_follows(self):
        from radar.people import first_list
        text = 'Found two.\n[{"name": "A"}]\n\nSources: [1] x\n```json\n[{"name": "B"}]\n```'
        self.assertEqual(first_list(text), [{"name": "A"}])

    def test_skips_brackets_that_are_not_the_list(self):
        from radar.people import first_list
        self.assertEqual(first_list('See [1] and [the site].\n[{"name": "A"}] done ]'), [{"name": "A"}])

    def test_no_list_is_empty(self):
        from radar.people import first_list
        self.assertEqual((first_list("nobody found"), first_list("[oops")), ([], []))


class AddPerson(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from radar import accounts as A
        d = Path(tempfile.mkdtemp())
        self.people, companies = d / "people.json", d / "companies.json"
        companies.write_text(json.dumps({"Acme Ltd": {"status": "active"}}))
        for target, f in ((A, "PEOPLE"), (A, "COMPANIES")):
            patcher = mock.patch.object(target, f, self.people if f == "PEOPLE" else companies)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_adds_a_person_by_hand(self):
        import json
        from radar import accounts as A, schema
        pid, created = A.add_person("Jo Bloggs", "acme ltd", role="VP Product", url="https://example.com/jo", kind="hiring_manager")
        self.assertEqual((pid, created), ("jo-bloggs@acme-ltd", True))
        p = json.loads(self.people.read_text())[pid]
        self.assertEqual((p["company"], p["stage"], p["source"], p["kind"]), ("Acme Ltd", "mapped", "manual", "hiring_manager"))
        self.assertEqual(schema.check_record("people", pid, p, {"Acme Ltd": {}}, {}), [])

    def test_adding_again_keeps_what_is_there(self):
        import json
        from radar import accounts as A
        A.add_person("Jo Bloggs", "Acme Ltd", role="VP Product")
        pid, created = A.add_person("Jo Bloggs", "Acme Ltd", role="CEO", url="https://example.com/jo")
        p = json.loads(self.people.read_text())[pid]
        self.assertEqual((created, p["role"], p["url"]), (False, "VP Product", "https://example.com/jo"))

    def test_refuses_unknown_account_or_bad_kind(self):
        from radar import accounts as A
        for args in (("Jo", "Nowhere"), ("", "Acme Ltd")):
            with self.assertRaises(ValueError):
                A.add_person(*args)
        with self.assertRaises(ValueError):
            A.add_person("Jo", "Acme Ltd", kind="friend")


class Service(unittest.TestCase):
    def test_buttons_and_worker_share_one_queue(self):
        """Started the way the Pi starts it, a job queued by the cockpit must actually run."""
        import subprocess
        import sys
        code = """
import runpy, sys, threading, time
from unittest import mock
sys.argv = ["radar.service"]
import radar.server, radar.service as svc
def fake_server():
    with mock.patch.object(svc, "_run_job", lambda job: (True, "ran")):
        radar.server.service.start("scan")
        time.sleep(1)
        print("RAN" if "scan" in radar.server.service.last else "STUCK", flush=True)
mock.patch.object(radar.server, "main", fake_server).start()
runpy.run_module("radar.service", run_name="__main__")
"""
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertIn("RAN", out.stdout, out.stderr)


class Targeting(unittest.TestCase):
    def setUp(self):
        use_sample(self)

    def test_where(self):
        from radar.matching import where
        for loc, want in (("Berlin / London", "London"), ("Remote, United Kingdom", "Remote"), ("Manchester, UK", "UK"), ("Paris, Europe", "Europe")):
            self.assertEqual(where(loc), want)

    def test_titles(self):
        from radar.matching import load_config, role_type, wanted_title
        c = load_config()
        keep = lambda t: wanted_title(t, c)
        for t in ("Senior Product Manager", "Forward Deployed Engineer", "Forward-Deployed PM", "Product Engineer", "Founding Product Engineer", "Applied AI Engineer", "Member of Deployed Staff"):
            self.assertTrue(keep(t), t)
        for t in ("Software Engineer", "Product Designer", "Product Marketing Manager", "Engineering Manager", "Product Operations Lead"):
            self.assertFalse(keep(t), t)
        self.assertEqual(role_type("Forward Deployed Engineer"), "Forward deployed")
        self.assertEqual(role_type("Product Engineer"), "Product engineer")
        self.assertEqual(role_type("Senior Product Manager"), "PM")
        self.assertEqual(role_type("Founding Product Manager"), "PM")
        self.assertEqual(role_type("Forward Deployed Product Manager"), "Forward deployed")

    def test_remote_locations(self):
        from radar.matching import load_config
        loc = load_config()["location_include"]
        for l in ("Remote", "London", "Remote - Worldwide", "Remote, United Kingdom", "Remote (EMEA)"):
            self.assertTrue(loc.search(l), l)
        for l in ("Remote - US", "San Francisco", "Remote, Canada"):
            self.assertFalse(loc.search(l), l)

    def test_plain_lists_are_read_as_people_expect(self):
        from radar.settings import to_pattern
        hit = lambda items, text: bool(re.search(to_pattern(items), text, re.I))
        self.assertTrue(hit("recruit*", "Recruitment Ltd"))
        self.assertFalse(hit("recruit", "Recruitment Ltd"))
        self.assertTrue(hit("*recruit*", "Prorecruiters"))
        self.assertFalse(hit("uk", "Dukes"))
        self.assertTrue(hit("forward deployed", "Forward-Deployed Engineer"))
        self.assertTrue(hit("few&far", "Few&Far"))
        self.assertTrue(hit("remote (exactly)", " Remote "))
        self.assertFalse(hit("remote (exactly)", "Remote, US"))
        self.assertFalse(hit("", "anything"))

    def test_older_settings_written_as_patterns_still_work(self):
        from radar.settings import to_pattern
        self.assertTrue(re.search(to_pattern(r"\b(pm|product)\b"), "Senior PM", re.I))

    def test_a_line_that_cant_be_read_is_refused(self):
        from radar import settings
        with self.assertRaises(ValueError):
            settings.validate("title_include", "(broken")

    def test_exceptions_stop_a_leave_out_word_catching_a_wanted_title(self):
        from radar.matching import load_config, wanted_title
        c = load_config()
        self.assertTrue(wanted_title("Forward-Deployed Engineer", c))
        self.assertFalse(wanted_title("Staff Engineer, Product", c))


class Distance(unittest.TestCase):
    CONNS = [{"name": "Ada Example", "url": "u", "company": "Acme Ltd", "position": "PM"}]

    def label(self, person, conns=()):
        return distance(paths_for(person, list(conns), {}, ["Globex"]))

    def test_estimates_are_marked(self):
        d = self.label({"name": "Pat", "company": "Acme"}, self.CONNS)
        self.assertEqual((d["label"], d["certain"]), ("~2nd", False))

    def test_confirmed_beats_estimate(self):
        d = self.label({"name": "Pat", "company": "Acme", "degree": 3}, self.CONNS)
        self.assertEqual((d["label"], d["certain"]), ("3rd+", True))

    def test_reply_means_in_touch(self):
        self.assertEqual(self.label({"name": "Pat", "company": "Acme", "stage": "replied"})["label"], "In touch")

    def test_no_route_is_cold(self):
        self.assertEqual(self.label({"name": "Pat", "company": "Other"})["label"], "3rd+")


if __name__ == "__main__":
    unittest.main()


class SharedBoard(unittest.TestCase):
    """A company whose roles sit on its parent's board (Base44 on Wix's) keeps only the titles its `only` pattern matches."""

    def test_only_keeps_matching_titles(self):
        import csv, tempfile
        from pathlib import Path
        from unittest import mock
        from radar import boards, matching, scan
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        path = Path(d.name) / "watchlist.csv"
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerows([["company", "ats", "slug", "sector", "only"], ["Base44", "fake", "wix", "ai-apps", "base44"], ["Wix", "fake", "wix", "web", ""]])
        board = lambda company, slug: [{"id": f"{company}:1", "company": company, "title": "Product Manager - Base44"},
                                       {"id": f"{company}:2", "company": company, "title": "Full Stack Engineer"}]
        with mock.patch.dict(boards.SOURCES, fake=board, clear=True), mock.patch.object(scan, "watchlist_path", lambda: path), \
             mock.patch.object(matching, "load_config", lambda: {"source_hn": False}), mock.patch.object(boards, "save_looked_up", lambda a: None), \
             mock.patch.object(scan, "DATA", Path(d.name)):
            jobs, errors = scan.fetch_all()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(j["title"] for j in jobs if j["company"] == "Base44"), ["Product Manager - Base44"])
        self.assertEqual(len([j for j in jobs if j["company"] == "Wix"]), 2)


class BotIdentity(unittest.TestCase):
    """Sites see who is asking: a name, the release and a link to this repo; and no host gets a burst of requests from one address."""

    def test_user_agent_names_the_repo_and_release(self):
        from radar import web
        self.assertRegex(web.ua()["User-Agent"], r"^gus/(\d+\.\d+\.\d+|dev) \(\+https://github\.com/SI-LC/gus; .+\)$")

    def test_version_without_git_is_dev(self):
        import subprocess
        from unittest import mock
        from radar import web
        with mock.patch.object(subprocess, "run", side_effect=OSError("no git")), mock.patch.object(web, "_version", (0.0, "")):
            self.assertEqual(web.version(), "dev")

    def test_a_tag_made_after_the_start_is_picked_up_within_the_hour(self):
        """The release is tagged after the Pi has started the new code, so the header must not be fixed at import."""
        from unittest import mock
        from radar import web
        with mock.patch.object(web, "_version", (0.0, "")):
            with mock.patch.object(web.subprocess, "run", lambda *a, **k: mock.Mock(stdout="v9.9.9\n")):
                self.assertEqual(web.version(), "9.9.9")
            with mock.patch.object(web.subprocess, "run", lambda *a, **k: mock.Mock(stdout="v9.9.10\n")):
                self.assertEqual(web.version(), "9.9.9", "cached for an hour")
                web._version = (0.0, web._version[1])  # the hour is up
                self.assertEqual(web.version(), "9.9.10", "read again")

    def test_requests_to_one_host_are_paced(self):
        import io
        from unittest import mock
        from radar import boards, web

        class Reply(io.BytesIO):
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        slept = []
        with mock.patch("urllib.request.urlopen", lambda req, timeout=30: Reply(b"ok")), mock.patch.object(boards.time, "sleep", slept.append), \
             mock.patch.dict(web._hosts, clear=True):
            web.fetch_bytes("https://boards.example.com/a/jobs")
            web.fetch_bytes("https://other.example.com/jobs")
            self.assertEqual(slept, [], "different hosts run side by side")
            web.fetch_bytes("https://boards.example.com/b/jobs")
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], web.HOST_PAUSE, delta=0.2)


class YourOwnSearch(unittest.TestCase):
    """Nothing in the code decides where or what you're looking for: places come from Settings, lists start as examples."""

    def test_news_counts_when_it_names_your_places_not_gus_authors(self):
        from radar import feeds
        from radar.settings import to_pattern
        berlin = re.compile(to_pattern("berlin\ngermany"), re.I)
        self.assertTrue(feeds.is_signal("Acme opens in Berlin", berlin))
        self.assertFalse(feeds.is_signal("Acme opens in London", berlin))
        self.assertFalse(feeds.is_signal("Acme opens in London"))
        self.assertTrue(feeds.is_signal("Acme raises a Series B"))

    def test_your_vc_list_starts_as_a_copy_of_the_example_and_is_then_yours(self):
        import tempfile
        from unittest import mock
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "vcs.example.csv").write_text("vc,portfolio,focus,jobs\nA,https://a.example,x,\n")
            with mock.patch.object(agent, "ROOT", root):
                self.assertEqual(agent.vcs_path().read_text(), (root / "vcs.example.csv").read_text())
                (root / "vcs.csv").write_text("vc,portfolio,focus,jobs\nMine,https://m.example,y,\n")
                self.assertIn("Mine", agent.vcs_path().read_text())

    def test_the_agents_are_told_no_region_or_role_of_gus_authors(self):
        from pathlib import Path
        prompts = Path(__file__).resolve().parent.parent / "prompts"
        text = "\n".join(p.read_text() for p in prompts.glob("*.md")) + agent.PROMPT + agent.INBOX_PROMPT
        for word in ("UK spelling", "European", "Sifted", "UKTN", "Balderton", "hires PMs"):
            self.assertNotIn(word, text)


class NothingMissed(unittest.TestCase):
    def setUp(self):
        use_sample(self)

    def test_a_big_smartrecruiters_board_is_read_to_the_end(self):
        from unittest import mock
        from radar import boards, web
        asked = []

        def reply(url):
            asked.append(url)
            start = int(url.split("offset=")[1])
            return {"totalFound": 250, "content": [{"id": n, "name": "Product Manager", "refNumber": "R7", "location": {"city": "London", "country": "gb"}}
                                                   for n in range(start, min(start + 100, 250))]}
        with mock.patch.object(web, "get_json", reply), mock.patch.object(boards, "PAGE_PAUSE", 0):
            jobs = boards.smartrecruiters("Wise", "wise")
        self.assertEqual((len(jobs), len(asked), len({j["id"] for j in jobs})), (250, 3, 250))
        self.assertEqual(jobs[0]["ref"], "R7")  # the vacancy's number, which a re-post keeps

    def test_an_ad_with_no_place_is_judged_by_its_title(self):
        from radar import matching
        cfg = matching.load_config()
        ad = {"title": "Senior Product Manager, London", "location": "", "description": ""}
        self.assertTrue(matching.matches(ad, cfg))
        self.assertFalse(matching.matches({**ad, "title": "Senior Product Manager, New York"}, cfg))
        self.assertFalse(matching.matches({**ad, "title": "Senior Product Manager"}, cfg))
        self.assertFalse(matching.matches({**ad, "location": "New York"}, cfg))  # a place that is given still decides


class NothingReadsThem(unittest.TestCase):
    """Watched companies with no board are handed to the agent, the candidate's own choices first."""

    def test_no_board_lists_the_unread_and_leaves_out_the_recently_read(self):
        from datetime import date, timedelta
        from radar import companies
        today, old = str(date.today()), str(date.today() - timedelta(days=10))
        c = {"Read by a board": {"status": "watched", "board": "ashby:x"},
             "Looked at today": {"status": "watched", "careers_checked": today},
             "Maybe": {"status": "maybe"}, "Vetoed": {"status": "watched", "vetoed": True},
             "Plain": {"status": "watched", "careers_checked": old}, "Tier A": {"status": "watched", "tier": "A"},
             "Favourite": {"status": "watched", "favourite": True, "tier": "B", "careers_checked": old}}
        self.assertEqual(companies.no_board(c), ["Favourite", "Tier A", "Plain"])

    def test_only_a_few_per_run(self):
        from radar import companies
        c = {f"Co {n}": {"status": "watched"} for n in range(30)}
        self.assertEqual(len(companies.no_board(c)), companies.CAREERS_PER_RUN)

    def test_the_agent_may_add_roles_only_through_the_checked_command(self):
        from pathlib import Path
        prompts = Path(__file__).resolve().parent.parent / "prompts"
        self.assertIn("jobs", agent.COMMANDS)
        for name in ("watchlist.md", "discover.md"):
            text = (prompts / name).read_text()
            self.assertIn("python3 -m radar.jobs add", text)
            self.assertIn("Never write a role into `data/jobs.json` by hand", text)

    def test_the_playbooks_tell_agents_which_source_an_event_takes(self):
        """An agent that found news by web search once wrote `web`, a role's source, which the schema turns down on an event."""
        from pathlib import Path
        from radar.schema import EVENT_SOURCES
        prompts = Path(__file__).resolve().parent.parent / "prompts"
        self.assertIn("agent", EVENT_SOURCES)
        self.assertNotIn("web", EVENT_SOURCES)
        self.assertIn("`agent` when you found it yourself", (prompts / "discover.md").read_text())
        self.assertIn('"source": "agent"', (prompts / "watchlist.md").read_text())
