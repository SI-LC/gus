"""Does the whole app still work? A fresh install, the real scan script and the real cockpit, with a made-up job board
and a fake claude command (evals/world.py). Runs in the code scan, so it needs no network and costs nothing."""
import json
import time
import unittest
import urllib.error

from evals.world import World


class BeforeAnyScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = World(new=True)
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


CV = "Made-up CV of a senior product manager in London, with years of roadmap work. " * 4


class BrandNew(unittest.TestCase):
    """A new install: paste a CV, get roles, and never see first run again."""

    @classmethod
    def setUpClass(cls):
        cls.world = World(new=True)
        cls.addClassCleanup(cls.world.close)
        cls.world.start()
        cls.needed_at_start = cls.world.get("/api/state")["setup"]["needed"]
        cls.world.post("/api/first-run", {"cv": CV, "favourites": ["Harborlight"]})
        cls.world.post("/api/first-run", {"go": True})
        for _ in range(100):
            cls.done = cls.world.get("/api/first-run")
            if cls.done["saved"] and cls.done["top"]:
                break
            time.sleep(.1)
        cls.calls_for_the_roles = cls.world.ai_calls()

    def test_1_it_opens_on_first_run(self):
        self.assertTrue(self.needed_at_start)

    def test_2_the_best_role_comes_first_with_a_reason_and_who_to_talk_to(self):
        best = self.done["top"][0]
        self.assertEqual((best["title"], best["score"]), ("Senior Product Manager", 9))
        self.assertEqual((best["why"], best["talk_to"]), ("Made-up reason for Senior Product Manager.", "Head of Product"))

    def test_3_ai_is_called_once_for_the_cv_and_once_per_small_batch(self):
        self.assertLessEqual(self.calls_for_the_roles, 3)

    def test_4_your_answers_become_settings_and_a_profile(self):
        values = self.world.get("/api/settings")["values"]
        self.assertEqual((values["sources_location"], values["salary_symbol"]), ("London", "£"))
        self.assertIn("product manager", values["title_include"])
        self.assertIn("## CV", self.world.get("/api/profile")["text"])
        watched = (self.world.app / "watchlist.csv").read_text()
        self.assertIn("Harborlight", watched)
        self.assertNotIn("Anthropic", watched)  # the example list isn't yours

    def test_5_a_company_you_love_is_kept_as_a_favourite(self):
        self.assertTrue(json.loads((self.world.app / "data" / "companies.json").read_text())["Harborlight"]["favourite"])

    def test_6_a_restart_doesnt_bring_it_back_or_let_it_overwrite_you(self):
        self.world.stop()
        self.world.start()
        self.assertFalse(self.world.get("/api/state")["setup"]["needed"])
        self.assertIn("error", self.world.post("/api/first-run", {"cv": "x" * 300, "go": True}))


class SigningIn(unittest.TestCase):
    def test_sign_in_with_claude_from_the_page(self):
        world = World(new=True, signed_out=True)
        self.addCleanup(world.close)
        world.start()
        world.post("/api/first-run", {})
        for _ in range(50):
            ai = world.get("/api/first-run")["ai"]
            if ai:
                break
            time.sleep(.1)
        self.assertFalse(ai["ok"])
        self.assertIn("made-up-sign-in", world.post("/api/first-run/login", {})["url"])
        self.assertTrue(world.post("/api/first-run/login", {"code": "made-up-code"})["ai"]["ok"])


class SkippedOnce(unittest.TestCase):
    def test_skipping_is_remembered_after_a_restart(self):
        world = World(new=True)
        self.addCleanup(world.close)
        world.start()
        world.post("/api/first-run/skip", {})
        world.stop()
        world.start()
        self.assertFalse(world.get("/api/state")["setup"]["needed"])
        self.assertTrue(world.get("/api/state")["setup"]["can_run"])


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
        self.world.post("/api/settings", {"changes": {"run_time": "09:00"}})
        self.world.stop()
        self.world.start()
        saved = self.roles()["Senior Product Manager"]
        self.assertEqual((saved["stage"], saved["note"]), ("shortlisted", "Worth a look"))
        self.assertIn("09:00", str(self.world.get("/api/settings")))


if __name__ == "__main__":
    unittest.main()
