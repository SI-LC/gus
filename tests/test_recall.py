"""Did Gus find the roles you found? Your own list is the answer key, and every miss gets a cause."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import recall, scan
from sample_search import use_sample


def role(company, title, **more):
    return {"company": company, "title": title, "stage": "new", "score": 7, "url": "https://x.example/1", **more}


def want(company, title):
    return {"company": company, "title": title, "seen": "2026-11-03", "note": ""}


class Titles(unittest.TestCase):
    def test_your_shorthand_is_the_boards_long_form(self):
        for mine, theirs in (("Senior PM (Growth)", "Senior Product Manager (Growth)"), ("Lead PM (UK)", "Lead product manager (UK)"),
                             ("PM, Enterprise (Senior/Staff)", "Product Manager, Enterprise (Staff/Principal-level)"),
                             ("Sr. Product Manager", "Senior Product Manager - London")):
            self.assertGreaterEqual(recall.closeness(mine, theirs)[0], recall.ENOUGH, mine)

    def test_a_different_job_with_the_same_words_isnt_the_role(self):
        for mine, theirs in (("Product Director", "Product Design Director"), ("PM, Pricing", "Senior Product Manager, Trust & Safety"),
                             ("Product Manager", "Product Marketing Manager"), ("Head of Product", "Head of Product Engineering")):
            self.assertLess(recall.closeness(mine, theirs)[0], recall.ENOUGH, mine)

    def test_the_exact_title_beats_a_longer_one(self):
        rows = [("Senior Product Director, Agents", 1), ("Product Director", 2)]
        self.assertEqual(recall.best(want("Acme", "Product Director"), rows)[1], 2)

    def test_one_names_words_start_the_others(self):
        self.assertTrue(recall.same_employer("Faculty", "Faculty AI"))
        self.assertTrue(recall.same_employer("Flo Health Ltd", "Flo Health"))
        self.assertFalse(recall.same_employer("Wise", "Otherwise"))
        self.assertFalse(recall.same_employer("", "Acme"))


class Checking(unittest.TestCase):
    def setUp(self):
        use_sample(self)
        self.cfg = scan.load_config()

    def check(self, expected, store=None, companies=None, watched=None, board=None):
        return recall.check([expected], store or {}, companies or {}, watched or {}, self.cfg,
                            (lambda ats, slug: board) if board is not None else (lambda ats, slug: []))[0]

    def test_a_role_gus_has_is_found_with_where_it_came_from(self):
        store = {"adzuna:1": role("Faculty AI", "Lead Product Manager", also={"mtp:jobs:9": {}})}
        r = self.check(want("Faculty", "Lead PM"), store)
        self.assertEqual((r["found"], r["reached_you"], r["id"], r["sources"]), (True, True, "adzuna:1", ["adzuna", "mtp"]))

    def test_a_role_skipped_by_its_score_was_found_but_didnt_reach_you(self):
        r = self.check(want("Spotify", "Senior PM, Core Experience"), {"lever:spotify:1": role("Spotify", "Senior Product Manager - Core Experience", stage="skipped", score=4)})
        self.assertEqual((r["found"], r["reached_you"], r["auto_skipped"]), (True, False, True))

    def test_a_company_nobody_reads(self):
        self.assertIn("isn't one Gus watches", self.check(want("Helix Data", "Principal PM"))["why"])
        self.assertIn("nothing reads this company", self.check(want("Revolut", "Product Manager"), companies={"Revolut": {"status": "watched"}})["why"])

    def test_a_board_that_fell_off_the_watchlist(self):
        r = self.check(want("Monzo", "Lead PM"), companies={"Monzo": {"status": "watched", "board": "greenhouse:monzo"}})
        self.assertIn("fallen off the watchlist", r["why"])

    def test_a_role_your_title_rules_drop(self):
        r = self.check(want("Omnea", "Product Director"), watched={"Omnea": ("ashby", "omnea")}, board=[("Product Director", "London"), ("Product Design Director", "London")])
        self.assertIn('nothing in Titles to include matches "Product Director"', r["why"])
        r = self.check(want("Acme", "Product Marketing Lead"), watched={"Acme": ("ashby", "acme")}, board=[("Product Lead, Marketing", "London")])
        self.assertIn("Titles to leave out", r["why"])

    def test_a_role_your_location_rules_drop(self):
        r = self.check(want("Acme", "Senior PM"), watched={"Acme": ("ashby", "acme")}, board=[("Senior Product Manager", "New York")])
        self.assertIn('your location rules drop it: "Senior Product Manager" is listed in "New York"', r["why"])

    def test_a_role_that_isnt_on_the_board(self):
        r = self.check(want("Wise", "PM, Support Experience"), watched={"Wise": ("smartrecruiters", "wise")}, board=[("Product Lead, Wise for Banks", "London")])
        self.assertIn("isn't on its board now (1 jobs listed)", r["why"])

    def test_a_role_on_the_board_that_fits_is_due_next_scan(self):
        r = self.check(want("Acme", "Senior PM"), watched={"Acme": ("ashby", "acme")}, board=[("Senior Product Manager", "London")])
        self.assertIn("the next scan should bring it in", r["why"])

    def test_a_board_that_wont_answer_or_cant_be_listed(self):
        def refuses(ats, slug):
            raise OSError("HTTP Error 403: Forbidden")
        r = recall.check([want("Acme", "Senior PM")], {}, {}, {"Acme": ("ashby", "acme")}, self.cfg, refuses)[0]
        self.assertIn("didn't answer: HTTP Error 403", r["why"])
        r = recall.check([want("LSEG", "Senior PM")], {}, {}, {"LSEG": ("workday", "lseg.wd3/Careers")}, self.cfg, recall.listing)[0]
        self.assertIn("can't be listed whole", r["why"])

    def test_offline_reads_no_board(self):
        r = recall.check([want("Acme", "Senior PM")], {}, {}, {"Acme": ("ashby", "acme")}, self.cfg, None)[0]
        self.assertIn("run without --offline", r["why"])

    def test_the_summary_counts_recall_and_groups_the_causes(self):
        results = [self.check(want("Faculty", "Lead PM"), {"a:1": role("Faculty", "Lead Product Manager")}),
                   self.check(want("Spotify", "Senior PM"), {"l:1": role("Spotify", "Senior Product Manager", stage="skipped", score=3)}),
                   self.check(want("Helix", "Principal PM")), self.check(want("Xicoia", "PM"))]
        s = recall.summarise(results)
        self.assertEqual((s["roles"], s["found"], s["recall"], s["reached_you"], s["skipped"], s["missed"]), (4, 2, 0.5, 1, 1, 2))
        self.assertEqual([len(v) for v in s["causes"].values()], [2])


class YourList(unittest.TestCase):
    def setUp(self):
        use_sample(self)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        (self.dir / "w.csv").write_text("company,ats,slug,sector\n")
        for name, value in (("EXPECTED", self.dir / "evals" / "expected.csv"), ("REPORTS", self.dir / "evals" / "recall")):
            p = mock.patch.object(recall, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("DATA", self.dir), ("JOBS", self.dir / "jobs.json"), ("WATCHLIST", self.dir / "w.csv")):
            p = mock.patch.object(scan, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_add_keeps_a_role_once_however_its_written(self):
        self.assertTrue(recall.add("Omnea", "Product Director", "2026-11-03"))
        self.assertFalse(recall.add("Omnea Ltd", "product director"))
        self.assertTrue(recall.add("Omnea", "Senior PM"))
        self.assertEqual([(r["company"], r["title"], r["seen"]) for r in recall.load_expected()][0], ("Omnea", "Product Director", "2026-11-03"))

    def test_a_run_reports_keeps_the_report_and_changes_no_roles(self):
        (self.dir / "jobs.json").write_text('{"a:1": {"company": "Faculty AI", "title": "Lead Product Manager", "stage": "new", "score": 7}}')
        before = (self.dir / "jobs.json").read_text()
        recall.add("Faculty", "Lead PM")
        recall.add("Helix Data", "Principal PM")
        with mock.patch("builtins.print"):
            s = recall.run(offline=True)
        self.assertEqual((s["found"], s["missed"], len(list((self.dir / "evals" / "recall").glob("*.json")))), (1, 1, 1))
        self.assertEqual((self.dir / "jobs.json").read_text(), before)

    def test_an_empty_list_says_how_to_start(self):
        with mock.patch("builtins.print") as out:
            recall.run(offline=True)
        self.assertIn("No roles listed yet", out.call_args.args[0])
