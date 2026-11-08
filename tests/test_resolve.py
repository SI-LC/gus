"""A company's board is found from its own careers address without asking; a board found by name alone waits for a person."""
import csv
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from radar import companies, resolve

HIT = ("ashby", "acme", 12)


class FindingBoards(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.data = Path(d.name)
        self.list = self.data / "watchlist.csv"
        self.list.write_text("company,ats,slug,sector\nOther,ashby,other,ai\n")
        for p in (mock.patch.object(resolve, "DATA", self.data), mock.patch.object(resolve, "watchlist_path", lambda: self.list),
                  mock.patch.object(companies, "COMPANIES", self.data / "companies.json"), mock.patch.object(companies, "watchlist_path", lambda: self.list),
                  mock.patch.object(resolve, "address_answers", lambda url: "gone" not in url)):
            p.start()
            self.addCleanup(p.stop)

    def run_auto(self, records, careers_hit=None, name_hit=None):
        (self.data / "companies.json").write_text(json.dumps(records))
        with mock.patch.object(resolve, "from_careers", lambda url: careers_hit), mock.patch.object(resolve, "resolve", lambda name: name_hit):
            out = resolve.auto()
        return out, json.loads((self.data / "companies.json").read_text())

    def rows(self):
        return [r["company"] for r in csv.DictReader(self.list.open())]

    def test_a_board_named_by_the_careers_address_is_watched_at_once(self):
        out, saved = self.run_auto({"Acme": {"status": "candidate", "careers_url": "https://acme.example/careers"}}, careers_hit=HIT)
        self.assertEqual((out["added"], self.rows(), saved["Acme"]["board"]), (["Acme"], ["Other", "Acme"], "ashby:acme"))
        self.assertNotIn("board_guess", saved["Acme"])

    def test_a_board_found_by_name_alone_waits_for_a_person(self):
        out, saved = self.run_auto({"Acme": {"status": "candidate"}}, name_hit=HIT)
        self.assertEqual((out["added"], out["guesses"], self.rows()), ([], ["Acme"], ["Other"]))
        self.assertEqual(saved["Acme"]["board_guess"], {"board": "ashby:acme", "url": "https://jobs.ashbyhq.com/acme", "open_roles": 12, "found": str(date.today())})

    def test_a_dead_careers_address_is_flagged_and_the_name_tried(self):
        out, saved = self.run_auto({"Acme": {"status": "candidate", "careers_url": "https://gone.example/careers"}}, careers_hit=HIT, name_hit=HIT)
        self.assertEqual((out["broken"], out["added"], out["guesses"]), (["Acme"], [], ["Acme"]))
        self.assertTrue(saved["Acme"]["careers_url_broken"])

    def test_a_rejected_guess_isnt_offered_again(self):
        out, saved = self.run_auto({"Acme": {"status": "candidate", "board_guess_rejected": ["ashby:acme"]}}, name_hit=HIT)
        self.assertEqual(out["guesses"], [])
        self.assertNotIn("board_guess", saved["Acme"])

    def test_a_name_is_tried_again_only_after_a_while(self):
        recent, old = str(date.today() - timedelta(days=2)), str(date.today() - timedelta(days=resolve.GUESS_DAYS + 1))
        out, _ = self.run_auto({"New": {"status": "candidate", "guess_checked": recent}, "Old": {"status": "candidate", "guess_checked": old}}, name_hit=HIT)
        self.assertEqual(out["guesses"], ["Old"])

    def test_companies_with_a_board_or_out_of_the_running_are_left_alone(self):
        out, _ = self.run_auto({"Has": {"status": "watched", "board": "lever:has"}, "Out": {"status": "rejected"}}, name_hit=HIT)
        self.assertEqual(out, {"added": [], "guesses": [], "broken": []})

    def test_accepting_a_guess_watches_the_board(self):
        _, _ = self.run_auto({"Acme": {"status": "candidate"}}, name_hit=HIT)
        self.assertTrue(companies.accept_board("Acme"))
        saved = companies.load()["Acme"]
        self.assertEqual((self.rows(), saved["board"], "board_guess" in saved), (["Other", "Acme"], "ashby:acme", False))
        self.assertFalse(companies.accept_board("Acme"))

    def test_rejecting_a_guess_remembers_it(self):
        _, _ = self.run_auto({"Acme": {"status": "candidate"}}, name_hit=HIT)
        self.assertTrue(companies.reject_board("Acme"))
        saved = companies.load()["Acme"]
        self.assertEqual((self.rows(), saved["board_guess_rejected"], "board_guess" in saved), (["Other"], ["ashby:acme"], False))


class Addresses(unittest.TestCase):
    def test_only_gone_means_gone(self):
        import urllib.error
        gone = urllib.error.HTTPError("https://x", 404, "Not Found", {}, None)
        blocked = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
        with mock.patch.object(resolve, "fetch_bytes", side_effect=gone):
            self.assertFalse(resolve.address_answers("https://x"))
        with mock.patch.object(resolve, "fetch_bytes", side_effect=blocked):
            self.assertTrue(resolve.address_answers("https://x"))
        with mock.patch.object(resolve, "fetch_bytes", side_effect=OSError):
            self.assertTrue(resolve.address_answers("https://x"))


if __name__ == "__main__":
    unittest.main()
