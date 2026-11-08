"""The cockpit's write paths for events and companies: /api/event and /api/company/add, through the handlers behind them."""
import csv
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from radar import accounts as A, events as E, resolve, server


class Events(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.events, self.companies = d / "events.json", d / "companies.json"
        self.companies.write_text(json.dumps({"Acme": {"status": "watched"}}))
        self.patches = [mock.patch.object(E, "EVENTS", self.events), mock.patch.object(A, "EVENTS", self.events),  # accounts keeps its own copy of the path
                        mock.patch.object(server, "COMPANIES", self.companies)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def stored(self):
        return json.loads(self.events.read_text())

    def test_an_event_added_by_hand_is_recorded_at_the_account_only(self):
        out = server.update_event({"account": "Acme", "type": "funding", "summary": "Series B", "url": "https://x/b", "date": str(date.today())})
        self.assertTrue(out["ok"])
        e = self.stored()[out["id"]]
        self.assertEqual((e["account"], e["type"], e["source"]), ("Acme", "funding", "manual"))
        self.assertIn("error", server.update_event({"account": "Nobody", "type": "funding", "url": "https://x", "date": "2026-01-01"}))
        self.assertIn("error", server.update_event({"account": "Acme", "type": "funding", "url": "not a link", "date": "2026-01-01"}))

    def test_dismissing_keeps_the_record_but_stops_it_counting(self):
        eid = server.update_event({"account": "Acme", "type": "launch", "summary": "x", "url": "https://x/l", "date": str(date.today())})["id"]
        self.assertTrue(server.update_event({"id": eid, "dismissed": True})["ok"])
        self.assertIn(eid, self.stored())
        self.assertEqual(E.for_account("Acme", self.stored()), [])
        self.assertTrue(server.update_event({"id": eid, "dismissed": False})["ok"])
        self.assertEqual(len(E.for_account("Acme", self.stored())), 1)

    def test_a_role_event_cannot_be_dismissed_as_it_follows_the_role(self):
        self.events.write_text(json.dumps({"role:gh:acme:1": {"account": "Acme", "type": "role_posted", "job_id": "gh:acme:1",
                                                              "url": "https://x/j", "date": str(date.today()), "summary": "PM (8/10)"}}))
        self.assertIn("error", server.update_event({"id": "role:gh:acme:1", "dismissed": True}))
        self.assertNotIn("closed", self.stored()["role:gh:acme:1"])

    def test_the_state_payload_lists_events_with_freshness_and_no_raw_companies(self):
        old = str(date.today() - timedelta(days=60))
        server.update_event({"account": "Acme", "type": "funding", "summary": "old", "url": "https://x/o", "date": old})
        server.update_event({"account": "Acme", "type": "post_hiring", "summary": "new", "url": "https://x/n", "date": str(date.today())})
        with mock.patch.object(server, "JOBS", self.companies.parent / "jobs.json"):
            st = server.state()
        self.assertNotIn("companies", st)
        self.assertEqual([e["fresh"] for e in st["events"]], [True, False])
        self.assertEqual(st["labels"]["role_stages"][0], "new")
        self.assertIn("funding", st["labels"]["event_types"])


class Companies(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.companies, self.watchlist = d / "companies.json", d / "watchlist.csv"
        self.companies.write_text(json.dumps({"Acme": {"status": "watched"}}))
        self.watchlist.write_text("company,ats,slug,sector,only\nAcme,greenhouse,acme,,\n")
        self.patches = [mock.patch.object(server, "COMPANIES", self.companies),
                        mock.patch("radar.resolve.watchlist_path", lambda: self.watchlist),
                        mock.patch("radar.links.watchlist_path", lambda: self.watchlist)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_a_company_with_a_board_joins_the_watchlist_and_becomes_an_account(self):
        with mock.patch.object(resolve, "resolve", lambda name, careers=None: ("lever", "wayve", 12)):
            out = server.add_company({"name": " Wayve ", "careers": "https://wayve.ai/careers"})
        self.assertEqual((out["name"], out["board"], out["open_roles"]), ("Wayve", "lever:wayve", 12))
        rows = list(csv.DictReader(self.watchlist.open()))
        self.assertEqual((rows[-1]["company"], rows[-1]["ats"], rows[-1]["slug"]), ("Wayve", "lever", "wayve"))
        c = json.loads(self.companies.read_text())["Wayve"]
        self.assertEqual((c["status"], c["board"], c["source"], c["careers_url"]), ("watched", "lever:wayve", "manual", "https://wayve.ai/careers"))

    def test_without_a_board_it_is_still_an_account_but_not_on_the_watchlist(self):
        with mock.patch.object(resolve, "resolve", lambda name, careers=None: None):
            out = server.add_company({"name": "Quiet Co"})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["board"])
        self.assertEqual(len(self.watchlist.read_text().splitlines()), 2)
        self.assertNotIn("board", json.loads(self.companies.read_text())["Quiet Co"])

    def test_a_known_company_or_a_bad_request_is_refused_before_any_lookup(self):
        with mock.patch.object(resolve, "resolve", side_effect=AssertionError("looked up")):
            self.assertIn("error", server.add_company({"name": "acme"}))
            self.assertIn("error", server.add_company({"name": ""}))
            self.assertIn("error", server.add_company({"name": "New", "careers": "new.com/jobs"}))


if __name__ == "__main__":
    unittest.main()
