"""Strong roles should lead to the employer's own ad, and a link only changes on a real match."""
import json
import tempfile
import unittest
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

    def test_adds_a_company_to_the_watchlist_once(self):
        j = role(board={"ats": "ashby", "slug": "acme"})
        self.assertTrue(links.watch(j))
        self.assertFalse(links.watch(j))
        self.assertIn("Acme Ltd,ashby,acme,discovered", self.list.read_text())

    def test_nothing_to_add_without_a_confirmed_board(self):
        self.assertFalse(links.watch(role()))


if __name__ == "__main__":
    unittest.main()
