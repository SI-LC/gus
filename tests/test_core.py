"""python3 -m unittest discover -s tests"""
import unittest
from datetime import datetime, timezone

import email
import re

from radar import agent, ai
from radar.network import distance, paths_for
from radar.service import due

def at(day, h, m=0):
    return datetime(2026, 11, day, h, m)


class AppIcon(unittest.TestCase):
    def test_every_icon_file_exists(self):
        import json
        from radar import server
        for name, _ in server.STATIC.values():
            self.assertTrue((server.ROOT / "radar" / "static" / name).exists(), name)
        for icon in json.loads(server.MANIFEST)["icons"]:
            self.assertIn(icon["src"], server.STATIC)


class Schedule(unittest.TestCase):
    CFG = {"run_time": "08:00"}

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
        with mock.patch.multiple(service, _scan=step("scan"), _agent_step=step, _command=lambda *a: step("score")(),
                                  _moves_this_week=lambda: moves,
                                  _watchlist_due=lambda c, s: watchlist_due, _counts=lambda: {"roles": 0, "strong": 0},
                                  _state=lambda: {}, _save=lambda s: None, load_config=lambda: {"source_gmail": bool(emails)}):
            service.progress.clear()
            ok, summary = service.full_run()
        return ok, summary, calls, {s["name"]: s["status"] for s in service.progress["steps"]}

    def test_runs_in_order(self):
        _, _, calls, _ = self.run_with(emails=1)
        self.assertEqual(calls, ["scan", "inbox", "discover", "watchlist", "score", "moves"])

    def test_skips_what_it_doesnt_need(self):
        _, _, calls, steps = self.run_with(moves=5, watchlist_due=False)
        self.assertEqual(calls, ["scan", "discover", "score"])
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


class Budget(unittest.TestCase):
    def test_zero_blocks_every_run(self):
        from unittest import mock
        with mock.patch("radar.notify.notify"):  # a test must never send a real notification
            with self.assertRaises(ai.BudgetReached):
                ai._check_budget({"ai_daily_budget_usd": 0})
            with self.assertRaises(ai.BudgetReached):
                ai._check_budget({})

    def test_room_left_allows_a_run(self):
        ai._check_budget({"ai_daily_budget_usd": 1000000})


class Gmail(unittest.TestCase):
    def test_query_only_covers_chosen_senders_and_subjects(self):
        q = agent.gmail_query({"inbox_senders": "linkedin.com\nashbyhq.com", "inbox_subjects": "interview", "inbox_days": 2})
        self.assertEqual(q, '(from:linkedin.com OR from:ashbyhq.com OR subject:"interview") newer_than:2d')

    def test_only_read_tools_are_allowed(self):
        self.assertEqual(agent.GMAIL_READ, ["mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Gmail__get_thread"])


class AgentLimits(unittest.TestCase):
    def test_email_agent_has_no_internet(self):
        self.assertFalse({"WebSearch", "WebFetch"} & set(agent.OFFLINE))

    def test_web_agents_cant_read_email(self):
        self.assertIn("mcp__claude_ai_Gmail", agent.NO_EMAIL)


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

    def test_closed_roles_stop_being_signals(self):
        from radar.accounts import events
        jobs = {"j": {"company": "Acme", "score": 9, "title": "PM", "url": "u", "ad_closed": "x", "stage": "new"}}
        self.assertEqual(events("Acme", {}, jobs), [])


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
    with mock.patch.object(svc, "_do", lambda job: (True, "ran")):
        radar.server.service.start("scan")
        time.sleep(1)
        print("RAN" if "scan" in radar.server.service.last else "STUCK", flush=True)
mock.patch.object(radar.server, "main", fake_server).start()
runpy.run_module("radar.service", run_name="__main__")
"""
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertIn("RAN", out.stdout, out.stderr)


class Targeting(unittest.TestCase):
    def test_where(self):
        from radar.scan import where
        for loc, want in (("Berlin / London", "London"), ("Remote, United Kingdom", "Remote"), ("Manchester, UK", "UK"), ("Paris, Europe", "Europe")):
            self.assertEqual(where(loc), want)

    def test_titles(self):
        import json
        from radar.scan import ROOT, role_type
        c = json.loads((ROOT / "config.json").read_text())
        keep = lambda t: bool(re.search(c["title_include"], t, re.I)) and not re.search(c["title_exclude"], t, re.I)
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
        import json
        from radar.scan import ROOT
        loc = json.loads((ROOT / "config.json").read_text())["location_include"]
        for l in ("Remote", "London", "Remote - Worldwide", "Remote, United Kingdom", "Remote (EMEA)"):
            self.assertTrue(re.search(loc, l, re.I), l)
        for l in ("Remote - US", "San Francisco", "Remote, Canada"):
            self.assertFalse(re.search(loc, l, re.I), l)


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
