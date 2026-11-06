"""A role outside your locations is never saved, so its details must not be fetched again on every scan."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import scan
from sample_search import use_sample

LIST = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


class RepeatRequests(unittest.TestCase):
    def setUp(self):
        use_sample(self)
        self.dir = Path(tempfile.mkdtemp())
        for name, value in (("DATA", self.dir), ("JOBS", self.dir / "jobs.json"), ("LOOKED_UP", self.dir / "seen.json"), ("WATCHLIST", self.dir / "w.csv")):
            patcher = mock.patch.object(scan, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with (self.dir / "w.csv").open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["company", "ats", "slug", "sector"])
            writer.writerow(["Acme", "greenhouse", "acme", "x"])
        self.office, self.updated, self.calls = "San Francisco", "2026-01-01", []

    def reply(self, url, timeout=30):
        self.calls.append(url)
        if url.endswith("/jobs"):
            return {"jobs": [{"id": 1, "title": "Product Manager", "absolute_url": "https://example.com", "updated_at": self.updated, "location": {"name": "Hybrid"}}]}
        return {"content": "<p>The ad</p>", "offices": [{"name": self.office}]}

    def scan(self):
        scan._known, scan._looked_up = None, None
        scan._listed.clear()
        cfg = {**scan.load_config(), "source_hn": False}
        with mock.patch.object(scan, "get_json", self.reply), mock.patch.object(scan, "load_config", lambda: cfg):
            jobs, _errors = scan.fetch_all()
        self.addCleanup(setattr, scan, "_looked_up", None)
        self.addCleanup(setattr, scan, "_known", None)
        details = [u for u in self.calls if u != LIST]
        self.calls.clear()
        return jobs, details

    def test_details_are_fetched_once(self):
        self.assertEqual(len(self.scan()[1]), 1)
        self.assertEqual(self.scan()[1], [])

    def test_a_role_edited_since_is_fetched_again_and_can_match(self):
        self.scan()
        self.office, self.updated = "London", "2026-02-01"
        jobs, details = self.scan()
        self.assertEqual(len(details), 1)
        self.assertEqual(jobs[0]["location"], "Hybrid / London")

    def test_if_your_locations_change_the_role_is_fetched_for_its_ad(self):
        self.scan()
        looked = {"gh:acme:1": {"updated": self.updated, "location": "London"}}  # as if you'd moved to where it is
        scan.LOOKED_UP.write_text(json.dumps(looked))
        jobs, details = self.scan()
        self.assertEqual((len(details), jobs[0]["description"]), (1, "The ad"))

    def test_a_board_that_didnt_answer_keeps_what_it_knows(self):
        self.scan()
        scan.save_looked_up(set())
        self.assertIn("gh:acme:1", json.loads(scan.LOOKED_UP.read_text()))


if __name__ == "__main__":
    unittest.main()
