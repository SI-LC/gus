"""A company's website is kept only when its own homepage shows it's theirs, and a website you set is never replaced."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from radar import server, websites

BOARD_PAGE = '<html><head><title>Acme</title></head><body><a href="https://jobs.ashbyhq.com/acme">Jobs</a></body></html>'
NAME_PAGE = "<html><head><title>Acme | Payments for everyone</title></head><body></body></html>"
OTHER_PAGE = "<html><head><title>This domain is for sale</title></head><body></body></html>"


class Belonging(unittest.TestCase):
    def test_a_homepage_linking_to_the_board_is_theirs(self):
        self.assertTrue(websites.belongs(BOARD_PAGE, "Acme", {"board": "ashby:acme"}))

    def test_with_a_board_the_name_alone_isnt_enough(self):
        self.assertFalse(websites.belongs(NAME_PAGE, "Acme", {"board": "ashby:acme"}))

    def test_the_sitemap_can_show_the_careers_address(self):
        rec = {"careers_url": "https://acme.com/careers"}
        self.assertTrue(websites.belongs(OTHER_PAGE, "Acme", rec, ["https://acme.com/careers", "https://acme.com/blog"]))

    def test_with_no_board_the_title_must_carry_the_name_word_for_word(self):
        self.assertTrue(websites.belongs(NAME_PAGE, "Acme", {}))
        self.assertFalse(websites.belongs(OTHER_PAGE, "Acme", {}))
        self.assertFalse(websites.belongs("<title>Acmetrics</title>", "Acme", {}))

    def test_candidates_come_from_what_gus_holds_then_guesses(self):
        rec = {"careers_url": "https://careers.acme-pay.com/jobs",
               "product_leaders": [{"url": "https://acmelabs.com/about"}, {"url": "https://www.linkedin.com/in/someone"}, {"url": "https://news.example/acme"}]}
        self.assertEqual(websites.candidates("Acme Labs", rec),
                         ["https://acme-pay.com/", "https://acmelabs.com/", "https://acmelabs.ai/", "https://acmelabs.io/"])

    def test_a_job_site_is_never_a_companys_own(self):
        self.assertEqual(websites.own_host("https://job-boards.greenhouse.io/acme"), "")


class Checking(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "companies.json"
        p = mock.patch.object(websites, "COMPANIES", self.path)
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, records, pages, gone=()):
        self.path.write_text(json.dumps(records))
        def fetch(url):
            if url not in pages:
                raise OSError("no such site")
            return pages[url]
        with mock.patch.object(websites.web, "polite_fetch", fetch), mock.patch.object(websites, "gone", lambda url: url in gone), \
             mock.patch.object(websites, "sitemap_of", lambda url: []):
            out = websites.run()
        return out, json.loads(self.path.read_text())

    def test_the_first_candidate_that_belongs_is_kept(self):
        out, saved = self.run_with({"Acme": {"status": "watched", "board": "ashby:acme"}},
                                   {"https://acme.com/": OTHER_PAGE, "https://acme.ai/": BOARD_PAGE})
        self.assertEqual((out["found"], saved["Acme"]["website"], saved["Acme"]["website_checked"]), (["Acme"], "https://acme.ai/", str(date.today())))

    def test_no_match_leaves_it_blank_and_waits_before_trying_again(self):
        _, saved = self.run_with({"Acme": {"status": "watched", "board": "ashby:acme"}}, {"https://acme.com/": OTHER_PAGE})
        self.assertNotIn("website", saved["Acme"])
        out, _ = self.run_with(saved, {"https://acme.com/": BOARD_PAGE})
        self.assertEqual(out["looked"], 0)

    def test_a_website_you_set_is_flagged_when_gone_but_never_replaced(self):
        recs = {"Acme": {"status": "watched", "board": "ashby:acme", "website": "https://old-acme.com/", "website_locked": True}}
        out, saved = self.run_with(recs, {"https://acme.com/": BOARD_PAGE}, gone={"https://old-acme.com/"})
        self.assertEqual((out["broken"], saved["Acme"]["website"], saved["Acme"]["website_broken"]), (["Acme"], "https://old-acme.com/", True))

    def test_a_gone_website_gus_found_is_replaced(self):
        recs = {"Acme": {"status": "watched", "board": "ashby:acme", "website": "https://old-acme.com/"}}
        out, saved = self.run_with(recs, {"https://acme.com/": BOARD_PAGE}, gone={"https://old-acme.com/"})
        self.assertEqual((out["broken"], saved["Acme"]["website"]), ([], "https://acme.com/"))
        self.assertNotIn("website_broken", saved["Acme"])

    def test_a_website_you_cleared_stays_clear(self):
        _, saved = self.run_with({"Acme": {"status": "watched", "board": "ashby:acme", "website_locked": True}}, {"https://acme.com/": BOARD_PAGE})
        self.assertNotIn("website", saved["Acme"])

    def test_companies_out_of_the_running_are_left_alone(self):
        old = str(date.today() - timedelta(days=websites.RECHECK_DAYS + 1))
        out, _ = self.run_with({"Out": {"status": "rejected"}, "Gone": {"status": "watched", "vetoed": True},
                                "Due": {"status": "watched", "website_checked": old}}, {})
        self.assertEqual(out["looked"], 1)


class SettingIt(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "companies.json"
        self.path.write_text(json.dumps({"Acme": {"status": "watched", "website": "https://wrong.com/", "website_broken": True}}))
        p = mock.patch.object(server, "COMPANIES", self.path)
        p.start()
        self.addCleanup(p.stop)

    def test_a_website_you_give_is_kept_and_locked(self):
        self.assertEqual(server.update_account({"name": "Acme", "website": "acme.com"}), {"ok": True})
        saved = json.loads(self.path.read_text())["Acme"]
        self.assertEqual((saved["website"], saved["website_locked"], "website_broken" in saved), ("https://acme.com", True, False))

    def test_clearing_it_says_there_is_none(self):
        server.update_account({"name": "Acme", "website": ""})
        saved = json.loads(self.path.read_text())["Acme"]
        self.assertEqual(("website" in saved, saved["website_locked"]), (False, True))

    def test_something_that_isnt_an_address_is_refused(self):
        self.assertIn("error", server.update_account({"name": "Acme", "website": "not a site"}))


if __name__ == "__main__":
    unittest.main()
