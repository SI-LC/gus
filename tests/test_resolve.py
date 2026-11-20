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
        with mock.patch.object(resolve, "from_careers", lambda url: careers_hit), mock.patch.object(resolve, "resolve", lambda name: name_hit), \
             mock.patch.object(resolve, "try_board", lambda ats, slug: None):  # no network: a board off the watchlist is checked before it goes back
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
        self.assertEqual(out, {"added": [], "guesses": [], "broken": [], "restored": []})

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


    # A board the scan has stopped reading goes back by itself, and the candidate's own choices outrank a fit score.

    def restore(self, records, answers=True):
        (self.data / "companies.json").write_text(json.dumps(records))
        with mock.patch.object(resolve, "from_careers", lambda url: None), mock.patch.object(resolve, "resolve", lambda name: None), \
             mock.patch.object(resolve, "try_board", lambda ats, slug: (ats, slug, 9) if answers else None):
            out = resolve.auto()
        return out, json.loads((self.data / "companies.json").read_text())

    def test_a_watched_account_whose_line_went_missing_is_read_again(self):
        out, _ = self.restore({"Monzo": {"status": "watched", "board": "greenhouse:monzo"}})
        self.assertEqual((out["restored"], self.rows()), (["Monzo"], ["Other", "Monzo"]))

    def test_a_favourite_parked_or_rejected_by_a_score_is_watched_again(self):
        out, saved = self.restore({"Harvey": {"status": "maybe", "favourite": True, "board": "ashby:harvey"},
                                   "Wayve": {"status": "rejected", "tier_locked": True, "board": "ashby:wayve"}})
        self.assertEqual((sorted(out["restored"]), saved["Harvey"]["status"], saved["Wayve"]["status"]), (["Harvey", "Wayve"], "watched", "watched"))

    def test_a_rejected_or_vetoed_account_stays_off(self):
        out, saved = self.restore({"Snyk": {"status": "rejected", "board": "ashby:snyk"},
                                   "Nope": {"status": "watched", "favourite": True, "vetoed": True, "board": "ashby:nope"}})
        self.assertEqual((out["restored"], self.rows(), saved["Snyk"]["status"]), ([], ["Other"], "rejected"))

    def test_a_board_that_no_longer_answers_isnt_put_back(self):
        out, _ = self.restore({"Monzo": {"status": "watched", "board": "greenhouse:monzo"}}, answers=False)
        self.assertEqual((out["restored"], self.rows()), ([], ["Other"]))

    def test_the_agent_is_told_to_keep_the_candidates_choices(self):
        text = (Path(__file__).resolve().parent.parent / "prompts" / "watchlist.md").read_text()
        self.assertIn("never dropped", text)


class Guessing(unittest.TestCase):
    """A name is tried on each vendor under a few likely spellings. Each wrong one is a request the vendor answers "no such board"."""

    def tried(self, name, slow=()):
        asked = []
        with mock.patch.object(resolve, "try_board", lambda ats, slug: asked.append((ats, slug))), \
             mock.patch.object(resolve, "unhurried", lambda who: who in slow):
            resolve.resolve(name)
        return asked

    def test_a_vendor_asked_slowly_gets_the_name_as_written_only(self):
        asked = self.tried("Acme Robotics", slow={"workable.com"})
        self.assertEqual(sorted(s for a, s in asked if a == "workable"), ["acme-robotics", "acmerobotics"])
        self.assertEqual(len([s for a, s in asked if a == "greenhouse"]), 7, "the others still get every spelling")

    def test_every_vendor_gets_every_spelling_when_none_is_slow(self):
        self.assertEqual(len(self.tried("Acme Robotics")), 7 * len(resolve.PROBES))

    def test_a_board_matching_one_word_of_a_name_is_a_guess_and_never_watched_unseen(self):
        """"harbour" is some shop's board, not Harbour Stock Exchange Group's. Adding a company or a role watches a board
        at once, so it asks for the name as written; the full run still finds the guess and shows it under Needs a look."""
        someone_elses = lambda ats, slug: (ats, slug, 3) if (ats, slug) == ("greenhouse", "harbour") else None
        with mock.patch.object(resolve, "try_board", someone_elses), mock.patch.object(resolve, "unhurried", lambda who: False):
            self.assertIsNone(resolve.resolve("Harbour Stock Exchange Group", as_written=True))
            self.assertEqual(resolve.resolve("Harbour Stock Exchange Group"), ("greenhouse", "harbour", 3))
            self.assertEqual(resolve.resolve("Harbour", as_written=True), ("greenhouse", "harbour", 3), "a one-word name is its own whole name")


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
