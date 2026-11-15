"""The log of changes to your roles (radar/changes.py): what the cockpit and triage write to it, and what reads it back.
Claude's side, the reason it must give and list_changes, is in test_mcp."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import changes, events as E, schema, server, triage


def role(i, **more):
    return {"id": i, "company": "Acme", "title": "Product Manager", "location": "London", "url": "https://x/" + i, "stage": "new", **more}


class Log(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.jobs = self.dir / "jobs.json"
        self.jobs.write_text(json.dumps({"a": role("a", stage="applied", note="Spoke to Sam at the meetup"), "b": role("b")}))
        p = mock.patch.object(server, "JOBS", self.jobs)
        p.start()
        self.addCleanup(p.stop)

    def logged(self):
        return changes.all_changes(self.jobs)

    def test_it_sits_beside_the_jobs_it_records(self):
        server.update_job({"id": "b", "stage": "shortlisted"})
        self.assertTrue((self.dir / "changes.jsonl").exists())

    def test_a_cockpit_change_is_yours_one_line_a_field(self):
        server.update_job({"id": "a", "stage": "skipped", "skip_reason": "pay"})
        self.assertEqual([(c["role"], c["field"], c["old"], c["new"], c["actor"]) for c in self.logged()],
                         [("a", "stage", "applied", "skipped", "you"), ("a", "skip_reason", None, "pay", "you")])
        self.assertNotIn("reason", self.logged()[0])

    def test_a_replaced_note_keeps_its_old_text(self):
        server.update_job({"id": "a", "note": "Sam left"})
        c = self.logged()[0]
        self.assertEqual((c["field"], c["old"], c["new"]), ("note", "Spoke to Sam at the meetup", "Sam left"))

    def test_nothing_is_logged_when_nothing_changed(self):
        server.update_job({"id": "a", "stage": "applied", "note": "Spoke to Sam at the meetup"})
        server.update_job({"id": "b", "note": ""})  # no note before, none after
        server.update_job({"id": "b", "title": "Something else"})  # refused: the ad's own words aren't yours to change
        self.assertEqual(self.logged(), [])

    def test_claude_and_its_reason_are_recorded(self):
        server.update_job({"id": "b", "stage": "shortlisted"}, actor="claude", why="the user asked")
        self.assertEqual((self.logged()[0]["actor"], self.logged()[0]["reason"]), ("claude", "the user asked"))

    def test_the_cockpit_gets_the_last_week_newest_first_with_each_role_named(self):
        server.update_job({"id": "b", "stage": "shortlisted"})
        server.update_job({"id": "b", "stage": "preparing"})
        old = changes.diff("a", {"stage": "new"}, {"stage": "applied"}, "you", at="2020-01-01T09:00:00+00:00")
        changes.append(old, self.jobs)
        out = server.recent_changes()
        self.assertEqual([(c["new"], c["title"], c["company"]) for c in out["changes"]], [("preparing", "Product Manager", "Acme"), ("shortlisted", "Product Manager", "Acme")])
        self.assertEqual(out["total"], 2)

    def test_a_line_cut_off_part_way_is_left_out(self):
        server.update_job({"id": "b", "stage": "shortlisted"})
        with open(self.dir / "changes.jsonl", "a") as f:
            f.write('{"id": "chg_')
        self.assertEqual(len(self.logged()), 1)

    def test_ids_sort_in_the_order_made(self):
        a = changes.diff("a", {}, {"note": "x"}, "you", at="2026-11-14T09:00:00+00:00")[0]["id"]
        b = changes.diff("a", {}, {"note": "y"}, "you", at="2026-11-15T08:00:00+00:00")[0]["id"]
        self.assertRegex(a, r"^chg_20261114T090000_[0-9a-f]{4}$")
        self.assertLess(a, b)


class TriageSkips(unittest.TestCase):
    def test_a_role_scoring_under_5_is_skipped_with_its_score_as_the_reason_and_no_history(self):
        d = Path(tempfile.mkdtemp())
        jobs, companies = d / "jobs.json", d / "companies.json"
        jobs.write_text(json.dumps({"a": role("a", score=None), "b": role("b", score=None)}))
        companies.write_text("{}")
        patches = [(triage, "JOBS", jobs), (triage, "COMPANIES", companies), (triage, "notify", mock.Mock()),
                   (triage, "load_config", lambda: {"notify_min_score": 8}), (E, "JOBS", jobs), (E, "EVENTS", d / "events.json"),
                   (E, "COMPANIES", companies), (E, "MOVES", d / "moves.json"), (E, "SIGNALS", d / "signals.json")]
        for target, attr, value in patches:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        profile = d / "profile.md"
        profile.write_text("Made-up candidate")
        reply = json.dumps([{"id": "a", "score": 3, "why": "Wrong area"}, {"id": "b", "score": 7, "why": "Close"}])
        with mock.patch.object(triage.ai, "run", return_value=reply):
            triage.score_batch(profile, list(json.loads(jobs.read_text()).values()))
        self.assertEqual([(c["role"], c["old"], c["new"], c["actor"], c["reason"]) for c in changes.all_changes(jobs)],
                         [("a", "new", "skipped", "triage", "scored 3/10")])
        self.assertNotIn("history", json.loads(jobs.read_text())["a"])  # history is what you did: "You skipped this" depends on it


class Schema(unittest.TestCase):
    def test_the_log_is_checked_line_by_line(self):
        d = Path(tempfile.mkdtemp())
        good = changes.diff("a", {}, {"note": "x"}, "claude", "asked")
        lines = [json.dumps(c) for c in good] + ["not json", json.dumps({**good[0], "field": "title"}),
                                                 json.dumps({**good[0], "actor": "robot"}), json.dumps({k: v for k, v in good[0].items() if k != "reason"})]
        (d / "changes.jsonl").write_text("\n".join(lines) + "\n")
        errs = schema.check(d)["changes"]
        self.assertEqual(len(errs), 4)
        self.assertTrue(errs[0].startswith("line 2"))
        self.assertIn("needs a reason", errs[3])


if __name__ == "__main__":
    unittest.main()
