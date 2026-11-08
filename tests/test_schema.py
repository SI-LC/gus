"""radar.schema check against made-up data files: good records pass, each kind of drift is named."""
import json
import tempfile
import unittest
from pathlib import Path

from radar import schema

GOOD = {
    "companies.json": {"Acme": {"status": "watched", "tier": "A", "fit": 8, "fit_why": "Right domain",
                                "events": [{"type": "funding", "summary": "Series B", "url": "https://x/1", "date": "2026-11-01"}]}},
    "people.json": {"sam-jones@acme": {"name": "Sam Jones", "company": "Acme", "stage": "mapped", "kind": "peer_pm", "warm": False}},
    "jobs.json": {"gh:acme:1": {"company": "Acme", "title": "PM", "url": "https://x/j", "stage": "new", "score": None, "source": "board"}},
    "moves.json": {"2026-W45:sam-jones@acme": {"week": "2026-W45", "account": "Acme", "person_id": "sam-jones@acme",
                                               "signal": {"type": "funding", "url": "https://x/1", "date": "2026-11-01"},
                                               "why_now": "a", "angle": "b", "draft": "c", "status": "todo", "outcome": None}},
    "signals.json": {"feed:1": {"source": "Lenny", "url": "https://x/s", "status": "new"}},
}


class Schema(unittest.TestCase):
    def run_check(self, **changes):
        d = Path(tempfile.mkdtemp())
        for name, recs in GOOD.items():
            data = json.loads(json.dumps(recs))
            for key, patch in changes.get(name, {}).items():
                data.setdefault(key, {}).update(patch) if patch is not None else data.pop(key, None)
            (d / name).write_text(json.dumps(data))
        return schema.check(d)

    def test_good_data_passes(self):
        self.assertEqual(self.run_check(), {})

    def test_missing_files_are_fine(self):
        self.assertEqual(schema.check(Path(tempfile.mkdtemp())), {})

    def test_bad_stage_and_score(self):
        out = self.run_check(**{"jobs.json": {"gh:acme:1": {"stage": "open", "score": 11}}})
        self.assertIn("gh:acme:1: stage='open' isn't one of ['new', 'shortlisted', 'preparing', 'applied', 'interviewing', 'closed', 'skipped']", out["jobs"])
        self.assertIn("gh:acme:1: score must be 1-10", out["jobs"])

    def test_bad_event_is_named_but_a_field_sync_migrates_is_not(self):
        out = self.run_check(**{"companies.json": {"Acme": {"why_it_fits": "x", "events": [{"type": "funding", "summary": "no link"}]}}})
        self.assertEqual(out["companies"], ["Acme: events[0] needs type, summary, url, date"])

    def test_move_must_point_at_known_account_and_person(self):
        out = self.run_check(**{"moves.json": {"2026-W45:sam-jones@acme": {"account": "Nope", "person_id": "who@nope"}}})
        self.assertEqual(out["moves"], ["2026-W45:sam-jones@acme: account 'Nope' isn't in companies.json",
                                        "2026-W45:sam-jones@acme: person 'who@nope' isn't in people.json"])

    def test_person_needs_name_company_stage(self):
        out = self.run_check(**{"people.json": {"sam-jones@acme": {"stage": "", "kind": "boss"}}})
        self.assertEqual(out["people"], ["sam-jones@acme: missing stage", "sam-jones@acme: kind='boss' isn't one of " + str(schema.PERSON_KINDS)])

    def test_broken_json_is_reported_not_raised(self):
        d = Path(tempfile.mkdtemp())
        (d / "jobs.json").write_text("{not json")
        self.assertTrue(schema.check(d)["jobs"][0].startswith("not valid JSON"))


if __name__ == "__main__":
    unittest.main()
