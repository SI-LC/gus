"""Does the whole app still work? A fresh install, the real scan script and the real cockpit, with a made-up job board
and a fake claude command (evals/world.py). Runs in the code scan, so it needs no network and costs nothing."""
import unittest
import urllib.error

from evals.world import World


class FirstRun(unittest.TestCase):
    """Before any scan has run."""

    @classmethod
    def setUpClass(cls):
        cls.world = World()
        cls.addClassCleanup(cls.world.close)
        cls.world.start()

    def test_every_read_works_with_nothing_saved(self):
        self.assertEqual(self.world.get("/api/state")["jobs"], [])
        self.assertIn("schema", self.world.get("/api/settings"))
        self.assertIn("text", self.world.get("/api/profile"))
        self.assertIn("running", self.world.get("/api/run"))

    def test_unknown_address_is_a_404(self):
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.world.get("/api/nothing-here")
        self.assertEqual(refused.exception.code, 404)

    def test_another_site_cant_change_anything(self):
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.world.post("/api/job", {"id": "x", "stage": "skipped"}, {"Origin": "https://example.com"})
        self.assertEqual(refused.exception.code, 403)


class FullRun(unittest.TestCase):
    """A role's whole journey: job board, filters, scoring, cockpit, and your decision surviving a restart."""

    @classmethod
    def setUpClass(cls):
        cls.world = World()
        cls.addClassCleanup(cls.world.close)
        cls.scan = cls.world.scan()
        cls.world.start()

    def roles(self):
        return {j["title"]: j for j in self.world.get("/api/state")["jobs"]}

    def test_1_every_step_of_a_scan_ran_without_crashing(self):
        self.assertEqual(self.scan.returncode, 0, self.scan.stderr[-800:])
        self.assertEqual(self.world.crashes(), {})

    def test_2_only_matching_roles_are_kept(self):
        self.assertEqual(set(self.roles()), {"Senior Product Manager", "Product Manager, Growth", "Product Owner"})

    def test_3_scores_and_reasons_reach_the_cockpit(self):
        best = self.roles()["Senior Product Manager"]
        self.assertEqual((best["score"], best["why"], best["stage"]), (9, "Made-up reason for Senior Product Manager.", "new"))

    def test_4_a_low_score_is_skipped_and_the_middle_stays(self):
        self.assertEqual(self.roles()["Product Owner"]["stage"], "skipped")
        self.assertEqual(self.roles()["Product Manager, Growth"]["stage"], "new")

    def test_5_a_second_scan_adds_nothing_and_uses_no_ai_on_roles(self):
        calls = self.world.ai_calls()
        self.assertEqual(self.world.scan().returncode, 0)
        self.assertEqual(self.world.crashes(), {})
        self.assertEqual(len(self.roles()), 3)
        self.assertEqual(self.world.ai_calls(), calls)

    def test_6_your_changes_survive_a_restart(self):
        role = self.roles()["Senior Product Manager"]
        self.world.post("/api/job", {"id": role["id"], "stage": "shortlisted", "note": "Worth a look"})
        self.world.post("/api/settings", {"changes": {"scan_times": "09:00"}})
        self.world.stop()
        self.world.start()
        saved = self.roles()["Senior Product Manager"]
        self.assertEqual((saved["stage"], saved["note"]), ("shortlisted", "Worth a look"))
        self.assertIn("09:00", str(self.world.get("/api/settings")))


if __name__ == "__main__":
    unittest.main()
