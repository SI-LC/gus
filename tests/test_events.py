"""radar.events: role events follow the roles, older shapes migrate, and the same link is one event."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from radar import events as E


class Sync(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.files = {"EVENTS": self.d / "events.json", "COMPANIES": self.d / "companies.json", "MOVES": self.d / "moves.json",
                      "JOBS": self.d / "jobs.json"}
        self.fresh = str(date.today() - timedelta(days=2))

    def write(self, name, data):
        self.files[name].write_text(json.dumps(data))

    def sync(self, jobs=None):
        with mock.patch.multiple(E, **self.files):
            out = E.sync(jobs)
        return out, json.loads(self.files["EVENTS"].read_text()) if self.files["EVENTS"].exists() else {}

    def test_a_good_open_role_is_an_event_until_it_closes(self):
        job = {"company": "Acme", "title": "PM", "url": "https://x/j", "score": 8, "stage": "new", "posted": self.fresh}
        out, ev = self.sync({"gh:acme:1": job})
        self.assertEqual(out["role_events"], 1)
        self.assertEqual(ev["role:gh:acme:1"]["summary"], "PM (8/10)")
        self.assertEqual(ev["role:gh:acme:1"]["date"], self.fresh)
        self.assertTrue(E.for_account("Acme", ev)[0]["fresh"])
        out, ev = self.sync({"gh:acme:1": dict(job, ad_closed="2026-11-07T00:00:00")})
        self.assertEqual(out["closed"], 1)
        self.assertEqual(ev["role:gh:acme:1"]["closed"], "2026-11-07T00:00:00")
        self.assertEqual(E.for_account("Acme", ev), [])
        out, _ = self.sync({"gh:acme:1": dict(job, ad_closed="2026-11-07T00:00:00")})
        self.assertEqual(out["closed"], 0, "closing is recorded once")

    def test_roles_under_the_threshold_or_skipped_are_not_events(self):
        _, ev = self.sync({"a": {"company": "Acme", "title": "PM", "url": "u", "score": E.ROLE_EVENT_SCORE - 1, "stage": "new"},
                           "b": {"company": "Acme", "title": "PM", "url": "u", "score": 9, "stage": "skipped"}})
        self.assertEqual(ev, {})

    def test_inline_account_events_and_pending_signals_move_off_the_account(self):
        self.write("COMPANIES", {"Acme": {"status": "watched", "pending_signals": [{"url": "x"}],
                                          "events": [{"type": "funding", "summary": "Series B", "url": "https://x/1", "date": "2026-11-01"},
                                                     {"type": "funding", "summary": "dupe", "url": "https://x/1", "date": "2026-11-01"},
                                                     {"type": "launch", "summary": "no link"}]}})
        out, ev = self.sync({})
        self.assertEqual(out["migrated"], 1)
        self.assertEqual(list(ev), ["acme:funding:2026-11-01"])
        self.assertEqual(ev["acme:funding:2026-11-01"]["source"], "account")
        self.assertEqual(json.loads(self.files["COMPANIES"].read_text()), {"Acme": {"status": "watched"}})

    def test_a_move_carrying_a_copied_signal_gets_an_event_id(self):
        self.write("MOVES", {"2026-W45:p": {"account": "Acme", "person_id": "p",
                                             "signal": {"type": "funding", "summary": "Raised", "url": "https://x/1", "date": "2026-11-01"}}})
        out, ev = self.sync({})
        self.assertEqual(out["moves"], 1)
        m = json.loads(self.files["MOVES"].read_text())["2026-W45:p"]
        self.assertEqual(m["event_id"], "acme:funding:2026-11-01")
        self.assertNotIn("signal", m)
        self.assertEqual(ev["acme:funding:2026-11-01"]["source"], "move")

    def test_add_dedupes_by_link_and_numbers_a_busy_day(self):
        with mock.patch.multiple(E, **self.files):
            a = E.add("Acme", "funding", "Series B", "https://x/1", "2026-11-01", "agent")
            b = E.add("Acme", "funding", "Series B again", "https://x/1", "2026-11-01", "agent")
            c = E.add("Acme", "funding", "Another round?", "https://x/2", "2026-11-01", "agent")
            with self.assertRaises(ValueError):
                E.add("Acme", "ipo", "x", "https://x/3", "2026-11-01")
            with self.assertRaises(ValueError):
                E.add("Acme", "funding", "x", "https://x/3", "soon")
        self.assertEqual((a, b, c), ("acme:funding:2026-11-01", "acme:funding:2026-11-01", "acme:funding:2026-11-01-2"))


if __name__ == "__main__":
    unittest.main()
