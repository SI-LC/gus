"""radar.schema check against made-up data files: good records pass, each kind of drift is named."""
import json
import tempfile
import unittest
from pathlib import Path

from radar import schema

GOOD = {
    "companies.json": {"Acme": {"status": "watched", "tier": "A", "fit": 8, "fit_why": "Right domain", "source": "discover"}},
    "events.json": {"acme:funding:2026-11-01": {"account": "Acme", "type": "funding", "summary": "Series B", "url": "https://x/1",
                                                "date": "2026-11-01", "source": "signal:feed:1"}},
    "people.json": {"sam-jones@acme": {"name": "Sam Jones", "company": "Acme", "stage": "mapped", "kind": "peer_pm", "warm": False}},
    "jobs.json": {"gh:acme:1": {"company": "Acme", "title": "PM", "url": "https://x/j", "stage": "new", "score": None, "source": "board"}},
    "moves.json": {"2026-W45:sam-jones@acme": {"week": "2026-W45", "account": "Acme", "person_id": "sam-jones@acme",
                                               "event_id": "acme:funding:2026-11-01",
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

    def test_fields_the_next_scan_migrates_are_not_flagged(self):
        out = self.run_check(**{"companies.json": {"Acme": {"why_it_fits": "x", "events": [{"type": "funding"}], "pending_signals": []}}})
        self.assertEqual(out, {})

    def test_bad_event_is_named(self):
        out = self.run_check(**{"events.json": {"acme:funding:2026-11-01": {"account": "Nope", "type": "ipo", "url": "x/1", "date": "Nov 1",
                                                                            "source": "guess"}}})
        self.assertEqual(out["events"], ["acme:funding:2026-11-01: type='ipo' isn't one of " + str(schema.EVENT_TYPES),
                                         "acme:funding:2026-11-01: account 'Nope' isn't in companies.json",
                                         "acme:funding:2026-11-01: url isn't a web link",
                                         "acme:funding:2026-11-01: date should be YYYY-MM-DD",
                                         "acme:funding:2026-11-01: source should start with one of " + str(schema.EVENT_SOURCES)])

    def test_move_must_point_at_a_recorded_event(self):
        out = self.run_check(**{"moves.json": {"2026-W45:sam-jones@acme": {"event_id": "acme:funding:1999-01-01"}}})
        self.assertEqual(out["moves"], ["2026-W45:sam-jones@acme: event 'acme:funding:1999-01-01' isn't in events.json"])

    def test_job_text_kind_is_one_of_two(self):
        out = self.run_check(**{"jobs.json": {"gh:acme:1": {"text_kind": "blurb"}}})
        self.assertEqual(out["jobs"], ["gh:acme:1: text_kind='blurb' isn't one of ['posting', 'summary']"])

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
