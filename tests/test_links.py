"""Strong roles should lead to the employer's own ad, and a link only changes on a real match."""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from radar import links


def role(**kw):
    return {"id": "jsearch:1", "source": "jsearch", "company": "Acme Ltd", "title": "Senior Product Manager", "score": 8, "stage": "new",
            "url": "https://jobsite.example/1", **kw}


class Finding(unittest.TestCase):
    def setUp(self):
        self.board = mock.patch.dict(links.SOURCES, greenhouse=lambda c, s: [{"title": "Sr. Product Manager (London)", "url": "https://acme.example/1"}])
        self.board.start()
        self.addCleanup(self.board.stop)

    def test_finds_the_same_title_on_the_company_board(self):
        with mock.patch.object(links, "resolve", return_value=("greenhouse", "acme", 4)):
            self.assertEqual(links.find("Acme Ltd", "Senior Product Manager", {}), {"url": "https://acme.example/1", "ats": "greenhouse", "slug": "acme"})

    def test_a_different_title_changes_nothing(self):
        with mock.patch.object(links, "resolve", return_value=("greenhouse", "acme", 4)):
            self.assertIsNone(links.find("Acme Ltd", "Head of Product", {}))

    def test_no_board_changes_nothing(self):
        with mock.patch.object(links, "resolve", return_value=None):
            self.assertIsNone(links.find("Acme Ltd", "Senior Product Manager", {}))

    def test_each_company_is_looked_up_once_a_run(self):
        boards = {}
        with mock.patch.object(links, "resolve", return_value=None) as look:
            links.find("Acme Ltd", "A", boards)
            links.find("Acme", "B", boards)
        self.assertEqual(look.call_count, 1)


class Choosing(unittest.TestCase):
    def test_only_strong_open_unchecked_source_roles(self):
        store = {"a": role(), "b": role(score=4), "c": role(source="board"), "d": role(stage="skipped"), "e": role(ad_closed="x"), "f": role(link_checked="x")}
        self.assertEqual(links.waiting(store), [store["a"]])


class AiSearch(unittest.TestCase):
    CFG = {"links_ai_min_score": 7, "links_ai_per_run": 2}

    def test_only_strong_roles_the_board_lookup_couldnt_place(self):
        store = {"a": role(link_checked="x"), "b": role(link_checked="x", score=6), "c": role(), "d": role(link_checked="x", link_kind="company"),
                 "e": role(link_checked="x", ai_link_checked="x"), "f": role(link_checked="x", stage="skipped")}
        self.assertEqual(links.ai_waiting(store, self.CFG), [store["a"]])

    def test_a_run_is_limited(self):
        store = {str(n): role(link_checked="x") for n in range(5)}
        self.assertEqual(len(links.ai_waiting(store, self.CFG)), 2)

    def test_job_sites_and_general_pages_are_refused(self):
        with mock.patch.object(links, "fetch_bytes"):
            for bad in ("https://www.linkedin.com/jobs/1", "https://uk.indeed.com/j/1", "https://www.adzuna.co.uk/jobs/1", "http://acme.example/j/1", "https://acme.example/", None, "nonsense"):
                self.assertFalse(links.usable(bad), bad)
            self.assertTrue(links.usable("https://jobs.ashbyhq.com/acme/1"))

    def test_a_page_thats_gone_is_refused_but_a_blocked_check_is_kept(self):
        gone = urllib.error.HTTPError("u", 404, "", {}, None)
        blocked = urllib.error.HTTPError("u", 403, "", {}, None)
        with mock.patch.object(links, "fetch_bytes", side_effect=gone):
            self.assertFalse(links.usable("https://acme.example/j/1"))
        with mock.patch.object(links, "fetch_bytes", side_effect=blocked):
            self.assertTrue(links.usable("https://acme.example/j/1"))

    def test_the_search_can_only_use_the_web(self):
        with mock.patch.object(links.ai, "run", return_value='{"url": null}') as run:
            self.assertIsNone(links.ai_find(role()))
        self.assertEqual(run.call_args.kwargs["tools"], ["WebSearch", "WebFetch"])
        self.assertIn("Read", run.call_args.kwargs["deny"])
        self.assertIn("Bash", run.call_args.kwargs["deny"])

    def test_a_reply_that_isnt_json_finds_nothing(self):
        with mock.patch.object(links.ai, "run", return_value="Sorry, no page."):
            self.assertIsNone(links.ai_find(role()))


class Saving(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.jobs = Path(d.name) / "jobs.json"
        self.list = Path(d.name) / "watchlist.csv"
        self.list.write_text("company,ats,slug,sector\nOther,ashby,other,ai\n")
        for p in (mock.patch.object(links, "JOBS", self.jobs), mock.patch.object(links, "watchlist_path", lambda: self.list)):
            p.start()
            self.addCleanup(p.stop)

    def saved(self):
        return json.loads(self.jobs.read_text())["jsearch:1"]

    def test_keeps_the_old_link_and_marks_the_new_one(self):
        self.jobs.write_text(json.dumps({"jsearch:1": role()}))
        with mock.patch.object(links, "find", return_value={"url": "https://acme.example/1", "ats": "ashby", "slug": "acme"}):
            links.main()
        j = self.saved()
        self.assertEqual((j["url"], j["source_url"], j["link_kind"]), ("https://acme.example/1", "https://jobsite.example/1", "company"))

    def test_a_fault_is_tried_again_next_time(self):
        self.jobs.write_text(json.dumps({"jsearch:1": role()}))
        with mock.patch.object(links, "find", side_effect=OSError):
            links.main()
        self.assertNotIn("link_checked", self.saved())

    def test_no_match_isnt_checked_again(self):
        self.jobs.write_text(json.dumps({"jsearch:1": role()}))
        with mock.patch.object(links, "find", return_value=None):
            links.main()
        j = self.saved()
        self.assertEqual((j["url"], "link_checked" in j), ("https://jobsite.example/1", True))

    def test_ai_search_is_off_by_default(self):
        self.jobs.write_text(json.dumps({"jsearch:1": role()}))
        with mock.patch.object(links, "find", return_value=None), mock.patch.object(links, "load_config", return_value={}), \
             mock.patch.object(links, "ai_find") as search:
            links.main()
        search.assert_not_called()

    def test_ai_search_saves_the_page_and_stops_at_the_daily_limit(self):
        self.jobs.write_text(json.dumps({"jsearch:1": role(link_checked="x"), "jsearch:2": role(id="jsearch:2", link_checked="x")}))
        with mock.patch.object(links, "ai_find", side_effect=["https://acme.example/j/1", links.ai.BudgetReached("limit")]):
            links.run_ai({"links_ai_min_score": 7, "links_ai_per_run": 5})
        saved = json.loads(self.jobs.read_text())
        self.assertEqual((saved["jsearch:1"]["url"], saved["jsearch:1"]["link_kind"]), ("https://acme.example/j/1", "company"))
        self.assertNotIn("ai_link_checked", saved["jsearch:2"])

    def test_adds_a_company_to_the_watchlist_once(self):
        j = role(board={"ats": "ashby", "slug": "acme"})
        self.assertTrue(links.watch(j))
        self.assertFalse(links.watch(j))
        self.assertIn("Acme Ltd,ashby,acme,discovered", self.list.read_text())

    def test_nothing_to_add_without_a_confirmed_board(self):
        self.assertFalse(links.watch(role()))


if __name__ == "__main__":
    unittest.main()
