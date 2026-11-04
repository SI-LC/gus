"""The eval tooling, with canned replies and made-up roles: no model, no network."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import evals, triage


def role(n, score=5, stage="new"):
    return {"id": f"r{n}", "company": "Acme", "title": "PM", "location": "London", "url": "https://example.com",
            "description": "Build things.", "score": score, "stage": stage}


class Summary(unittest.TestCase):
    def test_a_good_set_passes(self):
        r = evals.summarise({"a": [8, 8, 7], "b": [3, 3, 3]}, {"a": "apply", "b": "no"})
        self.assertTrue(r["passed"])
        self.assertEqual(r["top_share"], 1)

    def test_one_skip_in_any_repeat_counts_as_a_miss(self):
        r = evals.summarise({"a": [8, 8, 4]}, {"a": "apply"})
        self.assertEqual(r["missed"], ["a"])
        self.assertFalse(r["passed"])

    def test_a_score_of_exactly_5_isnt_a_miss(self):
        self.assertEqual(evals.summarise({"a": [5, 5, 5]}, {"a": "apply"})["missed"], [])

    def test_too_many_unwanted_roles_at_the_top_fail(self):
        scores = {"a": [8], "b": [8], "c": [8]}
        r = evals.summarise(scores, {"a": "maybe", "b": "no", "c": "no"})
        self.assertAlmostEqual(r["top_share"], 1 / 3)
        self.assertFalse(r["passed"])

    def test_top_share_at_the_limit_passes(self):
        scores = {str(n): [8] for n in range(10)}
        labels = {str(n): "apply" if n < 7 else "no" for n in range(10)}
        self.assertTrue(evals.summarise(scores, labels)["passed"])

    def test_an_ineligible_role_at_the_top_fails(self):
        scores = {str(n): [8] for n in range(9)}
        labels = {str(n): "apply" for n in range(8)} | {"8": "not eligible"}
        r = evals.summarise(scores, labels)
        self.assertEqual(r["not_eligible_top"], ["8"])
        self.assertFalse(r["passed"])

    def test_injection_is_judged_against_the_same_role(self):
        labels = {"a": "no"}
        self.assertFalse(evals.summarise({"a": [3, 3, 3], "a:injected": [3, 10, 3]}, labels)["passed"])
        self.assertTrue(evals.summarise({"a": [3, 3, 3], "a:injected": [4, 3, 4]}, labels)["passed"])

    def test_a_role_with_no_score_fails(self):
        r = evals.summarise({"a": [], "b": [3]}, {"a": "no", "b": "no"})
        self.assertEqual(r["unscored"], ["a"])
        self.assertFalse(r["passed"])

    def test_maybes_and_noise_inform_but_dont_fail(self):
        r = evals.summarise({"a": [4, 4, 4], "b": [2, 3, 5]}, {"a": "maybe", "b": "no"})
        self.assertEqual((r["maybe_skipped"], r["noisy"]), (["a"], ["b"]))
        self.assertTrue(r["passed"])


class JobPart(unittest.TestCase):
    def test_skips_the_company_story_with_or_without_line_breaks(self):
        for ad in ("We build rockets.\n\nABOUT THE ROLE\n\nOwn the roadmap.", "We build rockets. About the role Own the roadmap.",
                   "We build rockets.\n\nWhat you’ll do\n\nOwn the roadmap."):
            with self.subTest(ad):
                self.assertNotIn("rockets", evals.job_part(ad + " Then more." * 5))
                self.assertIn("Own the roadmap", evals.job_part(ad + " Then more." * 5))

    def test_shows_the_whole_ad_when_it_cant_find_the_job(self):
        self.assertEqual(evals.job_part("We build rockets. Own the roadmap."), "We build rockets. Own the roadmap.")

    def test_a_late_match_is_ignored(self):
        ad = "Own the roadmap. " * 20 + "Responsibilities may change."
        self.assertEqual(evals.job_part(ad), ad)


class Sampling(unittest.TestCase):
    def jobs(self):
        roles = [role(n, score=1 + n % 10) for n in range(100)] + [role(200, score=2, stage="interviewing")]
        return {r["id"]: r for r in roles}

    def test_takes_what_you_moved_forward_and_a_spread(self):
        picked = evals.choose(self.jobs())
        scores = [r["score"] for r in picked]
        self.assertEqual(len(picked), evals.SAMPLE)
        self.assertEqual(len({r["id"] for r in picked}), evals.SAMPLE)
        self.assertIn("r200", [r["id"] for r in picked])
        self.assertGreaterEqual(sum(s >= 7 for s in scores), 8)
        self.assertGreaterEqual(sum(4 <= s <= 6 for s in scores), 12)
        self.assertGreaterEqual(sum(s < 4 for s in scores), 10)

    def test_many_moved_forward_cant_crowd_out_low_scores(self):
        jobs = {r["id"]: r for r in [role(n, score=8, stage="shortlisted") for n in range(30)] + [role(n, score=2) for n in range(100, 160)]}
        self.assertGreaterEqual(sum(r["score"] < 4 for r in evals.choose(jobs)), 10)

    def test_is_repeatable(self):
        self.assertEqual(evals.choose(self.jobs()), evals.choose(self.jobs()))

    def test_ignores_roles_it_cant_judge(self):
        jobs = {"a": {**role(1), "description": ""}, "b": role(2, score=None), "c": role(3)}
        self.assertEqual([r["id"] for r in evals.choose(jobs)], ["r3"])

    def test_a_small_list_is_fine(self):
        self.assertEqual(len(evals.choose({r["id"]: r for r in (role(1, 2), role(2, 9))})), 2)


class Tool(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "profile").mkdir()
        (self.d / "profile" / "profile.md").write_text("Made-up candidate")
        self.jobs = self.d / "jobs.json"
        self.jobs.write_text('{"r1": {"stage": "new"}}')
        self.notify = mock.Mock()
        patches = [mock.patch.object(evals, "ROLES", self.d / "roles.json"), mock.patch.object(evals, "LABELS", self.d / "labels.json"),
                   mock.patch.object(evals, "OLD_LABELS", self.d / "labels.old.json"),
                   mock.patch.object(evals, "RUNS", self.d / "runs"), mock.patch.object(evals, "ROOT", self.d),
                   mock.patch.object(triage, "JOBS", self.jobs), mock.patch.object(triage, "notify", self.notify)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def seed(self, labels):
        roles = [role(n) for n in range(len(labels))]
        (self.d / "roles.json").write_text(json.dumps(roles))
        (self.d / "labels.json").write_text(json.dumps({r["id"]: v for r, v in zip(roles, labels)}))

    def reply(self, prompt, **_):
        batch = json.loads(prompt[prompt.index("## Roles (JSON)\n") + 16:prompt.index("\n\nFor each role")])
        return json.dumps([{"id": b["id"], "score": 3, "why": "x"} for b in batch])

    def test_label_saves_each_answer_and_hides_the_score(self):
        (self.d / "roles.json").write_text(json.dumps([role(1, score=9), role(2, score=9), role(3)]))
        with mock.patch("builtins.input", side_effect=["x", "a", "l", "q"]):
            evals.label()
        self.assertEqual(json.loads((self.d / "labels.json").read_text()), {"r1": "apply", "r2": "not eligible"})
        self.assertNotIn("9", self.out.getvalue())

    def test_label_resumes_where_you_stopped(self):
        self.seed(["apply"])
        (self.d / "roles.json").write_text(json.dumps([role(0), role(1)]))
        with mock.patch("builtins.input", side_effect=["n"]) as asked:
            evals.label()
        self.assertEqual(asked.call_count, 1)

    def test_label_shows_the_job_and_d_shows_the_full_ad(self):
        ad = "We build rockets. ABOUT THE ROLE Own the roadmap. " + "More detail. " * 300 + "The last line."
        (self.d / "roles.json").write_text(json.dumps([{**role(1), "description": ad}]))
        with mock.patch("builtins.input", side_effect=["q"]):
            evals.label()
        first = self.out.getvalue()
        self.assertIn("Own the roadmap", first)
        self.assertNotIn("rockets", first)
        self.assertNotIn("The last line.", first)
        with mock.patch("builtins.input", side_effect=["d", "n"]) as asked:
            evals.label()
        self.assertIn("The last line.", self.out.getvalue())
        self.assertEqual(asked.call_count, 2)
        self.assertEqual(json.loads((self.d / "labels.json").read_text()), {"r1": "no"})

    def test_label_asks_about_the_role_not_the_company(self):
        (self.d / "roles.json").write_text(json.dumps([role(1)]))
        with mock.patch("builtins.input", side_effect=["q"]):
            evals.label()
        self.assertIn("Would you apply for this role today?", self.out.getvalue())

    def test_restart_keeps_your_old_answers_and_asks_everything_again(self):
        self.seed(["apply"])
        with mock.patch("builtins.input", side_effect=["n"]) as asked:
            evals.label(fresh=True)
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(json.loads((self.d / "labels.old.json").read_text()), {"r0": "apply"})
        self.assertEqual(json.loads((self.d / "labels.json").read_text()), {"r0": "no"})

    def test_run_scores_every_role_each_repeat_with_injected_copies(self):
        self.seed(["no"] * 20)
        with mock.patch.object(triage.ai, "run", side_effect=self.reply) as run, mock.patch.object(evals, "load_config", dict):
            self.assertTrue(evals.run_evals(repeats=2))
        self.assertEqual(run.call_count, 4)  # 23 cases need 2 batches, twice
        report = json.loads(next((self.d / "runs").glob("*.json")).read_text())
        self.assertEqual(len(report["scores"]), 23)
        self.assertTrue(all(len(s) == 2 for s in report["scores"].values()))
        self.assertIn(evals.INJECTION.strip()[:40], run.call_args_list[0][0][0] + run.call_args_list[1][0][0])

    def test_run_never_saves_to_your_roles_or_sends_alerts(self):
        self.seed(["apply"])
        with mock.patch.object(triage.ai, "run", return_value='[{"id": "r0", "score": 9, "why": "x"}]'), \
                mock.patch.object(evals, "load_config", dict):
            evals.run_evals(repeats=1)
        self.assertEqual(self.jobs.read_text(), '{"r1": {"stage": "new"}}')
        self.notify.assert_not_called()

    def test_run_needs_labels_and_stops_on_an_unreadable_reply(self):
        with self.assertRaises(SystemExit):
            evals.run_evals()
        self.seed(["no"])
        with mock.patch.object(triage.ai, "run", return_value="Sorry"), self.assertRaises(SystemExit):
            evals.run_evals()


if __name__ == "__main__":
    unittest.main()
