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
                      "JOBS": self.d / "jobs.json", "SIGNALS": self.d / "signals.json"}
        self.fresh = str(date.today() - timedelta(days=2))

    def write(self, name, data):
        self.files[name].write_text(json.dumps(data))

    def sync(self, jobs=None):
        with mock.patch.multiple(E, **self.files):
            out = E.sync(jobs)
        return out, json.loads(self.files["EVENTS"].read_text()) if self.files["EVENTS"].exists() else {}

    def test_a_good_open_role_is_an_event_until_it_closes(self):
        self.write("COMPANIES", {"Acme": {"status": "watched"}})
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

    def test_roles_under_the_threshold_skipped_or_at_no_account_are_not_events(self):
        self.write("COMPANIES", {"Acme": {"status": "watched"}})
        self.write("EVENTS", {"role:c": {"account": "Stranger", "type": "role_posted", "url": "u", "date": "2026-11-01"}})
        out, ev = self.sync({"a": {"company": "Acme", "title": "PM", "url": "u", "score": E.ROLE_EVENT_SCORE - 1, "stage": "new"},
                             "b": {"company": "Acme", "title": "PM", "url": "u", "score": 9, "stage": "skipped"},
                             "c": {"company": "Stranger", "title": "PM", "url": "u", "score": 9, "stage": "new"}})
        self.assertEqual(ev, {})
        self.assertEqual(out["dropped"], 1)

    def test_inline_account_events_and_pending_signals_move_off_the_account(self):
        self.write("SIGNALS", {"feed:1": {"source": "Sifted", "url": "https://x/s", "status": "new"}})
        self.write("COMPANIES", {"Acme": {"status": "watched", "reassess": True, "pending_signals": [{"url": "https://x/s", "title": "Acme raises"}],
                                          "events": [{"type": "funding", "summary": "Series B", "url": "https://x/1", "date": "2026-11-01"},
                                                     {"type": "funding", "summary": "dupe", "url": "https://x/1", "date": "2026-11-01"},
                                                     {"type": "launch", "summary": "no link"}]}})
        out, ev = self.sync({})
        self.assertEqual(out["migrated"], 1)
        self.assertEqual(list(ev), ["acme:funding:2026-11-01"])
        self.assertEqual(ev["acme:funding:2026-11-01"]["source"], "account")
        self.assertEqual(json.loads(self.files["COMPANIES"].read_text()), {"Acme": {"status": "watched", "reassess": True}})
        self.assertEqual(out["signals"], 1)
        self.assertEqual(json.loads(self.files["SIGNALS"].read_text())["feed:1"]["company"], "Acme")

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


class Alert(unittest.TestCase):
    """Fresh hiring news at an A or B account is pushed once; the rest is left alone."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.events, self.companies, self.jobs = self.d / "events.json", self.d / "companies.json", self.d / "jobs.json"
        self.companies.write_text(json.dumps({"Acme": {"status": "watched", "tier": "A"}, "Tiny": {"status": "watched", "tier": "C"}}))
        self.today, self.pushed = str(date.today()), []
        self.patches = [mock.patch.multiple(E, EVENTS=self.events, COMPANIES=self.companies, JOBS=self.jobs),
                        mock.patch("radar.notify.notify", lambda *a: self.pushed.append(a)),
                        mock.patch("radar.settings.load", lambda: {"notify_news": True, "notify_min_score": 8})]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def ev(self, account, type_, added=None, **extra):
        return {"account": account, "type": type_, "summary": f"{type_} at {account}", "url": f"https://x/{account}/{type_}",
                "date": self.today, "added": added or self.today, **extra}

    def test_hiring_news_at_an_a_or_b_account_is_pushed_once(self):
        self.events.write_text(json.dumps({"a": self.ev("Acme", "post_hiring"), "b": self.ev("Acme", "funding"),
                                           "c": self.ev("Tiny", "leader_hire"),
                                           "d": self.ev("Acme", "leader_hire", added=str(date.today() - timedelta(days=10)))}))
        self.assertEqual(E.alert(), ["a"])
        self.assertEqual(len(self.pushed), 1)
        self.assertIn("Posted about hiring · Acme", self.pushed[0][0])
        self.assertEqual(json.loads(self.events.read_text())["a"]["notified"], self.today)
        self.assertEqual(E.alert(), [])  # not twice

    def test_a_role_event_already_covered_by_the_role_alert_is_not_pushed_again(self):
        self.jobs.write_text(json.dumps({"gh:acme:1": {"score": 9}, "gh:acme:2": {"score": 7}}))
        self.events.write_text(json.dumps({"role:gh:acme:1": self.ev("Acme", "role_posted", job_id="gh:acme:1"),
                                           "role:gh:acme:2": self.ev("Acme", "role_posted", job_id="gh:acme:2")}))
        self.assertEqual(E.alert(), ["role:gh:acme:2"])
        self.assertEqual(json.loads(self.events.read_text())["role:gh:acme:1"]["notified"], "role alert")

    def test_off_in_settings_pushes_nothing(self):
        self.events.write_text(json.dumps({"a": self.ev("Acme", "post_hiring")}))
        with mock.patch("radar.settings.load", lambda: {"notify_news": False}):
            self.assertEqual(E.alert(), [])
        self.assertEqual(self.pushed, [])

    def test_a_flood_becomes_five_pushes_and_one_summary(self):
        self.events.write_text(json.dumps({f"e{i}": self.ev("Acme", "post_hiring") | {"url": f"https://x/{i}"} for i in range(8)}))
        self.assertEqual(len(E.alert()), 8)
        self.assertEqual(len(self.pushed), 6)
        self.assertIn("3 more hiring signals", self.pushed[-1][0])
