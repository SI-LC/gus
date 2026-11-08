"""Pruning keeps the files small and never touches anything the candidate acted on."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from radar import prune, scan

TODAY = date(2026, 11, 8)
CFG = {"prune_after_days": 60}


def role(**more):
    return {"company": "Acme", "title": "PM", "url": "u", "stage": "new", "description": "long ad text", **more}


class Roles(unittest.TestCase):
    def test_an_old_closed_role_nobody_touched_loses_its_text(self):
        store = {"a": role(ad_closed="2026-08-01"), "b": role(stage="skipped", history=[{"stage": "skipped", "at": "2026-07-01T10:00:00"}])}
        self.assertEqual(sorted(prune.prunable_roles(store, date(2026, 9, 9))), ["a", "b"])

    def test_live_recent_noted_or_progressed_roles_are_kept(self):
        store = {"live": role(), "recent": role(ad_closed="2026-11-01"), "noted": role(ad_closed="2026-01-01", note="liked it"),
                 "folder": role(stage="closed", ad_closed="2026-01-01", folder="/x"), "applied": role(stage="applied", ad_closed="2026-01-01"),
                 "interviewing": role(stage="interviewing", ad_closed="2026-01-01"), "already": role(ad_closed="2026-01-01", description="")}
        self.assertEqual(prune.prunable_roles(store, date(2026, 9, 9)), [])

    def test_feed_dates_in_rfc_2822_are_understood(self):
        self.assertEqual(prune.when("Wed, 30 Sep 2026 17:00:20 +0000"), date(2026, 9, 30))
        self.assertEqual(prune.when("2025-06-10T15:13:57.000Z"), date(2025, 6, 10))
        self.assertIsNone(prune.when("not a date"))


class SignalsAndEvents(unittest.TestCase):
    def test_old_ignored_signals_go_and_the_rest_stay(self):
        signals = {"old": {"status": "ignored", "first_seen": "2026-01-01"}, "new": {"status": "new", "first_seen": "2026-01-01"},
                   "fresh": {"status": "ignored", "first_seen": "2026-11-01"}, "became": {"status": "processed", "first_seen": "2026-01-01", "event_id": "e"},
                   "undated": {"status": "ignored"}}
        self.assertEqual(prune.prunable_signals(signals, date(2026, 9, 9)), ["old"])

    def test_closed_events_go_unless_a_move_points_at_them(self):
        ev = {"gone": {"closed": "2026-01-01"}, "used": {"closed": "2026-01-01"}, "open": {}, "recent": {"closed": "2026-11-01"}}
        moves = {"m": {"event_id": "used"}}
        self.assertEqual(prune.prunable_events(ev, moves, date(2026, 9, 9)), ["gone"])


class EndToEnd(unittest.TestCase):
    def test_dry_run_counts_and_apply_archives(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            files = {"jobs": data / "jobs.json", "signals": data / "signals.json", "events": data / "events.json", "moves": data / "moves.json"}
            files["jobs"].write_text(json.dumps({"a": role(ad_closed="2026-01-01"), "b": role()}))
            files["signals"].write_text(json.dumps({"s": {"status": "ignored", "first_seen": "2026-01-01"}, "t": {"status": "new"}}))
            files["events"].write_text(json.dumps({"e": {"closed": "2026-01-01"}, "f": {}}))
            files["moves"].write_text("{}")
            patches = [mock.patch.object(scan, "JOBS", files["jobs"]), mock.patch.object(prune, "JOBS", files["jobs"]),
                       mock.patch.object(prune, "SIGNALS", files["signals"]), mock.patch.object(prune, "EVENTS", files["events"]),
                       mock.patch.object(prune, "MOVES", files["moves"]), mock.patch.object(prune, "ARCHIVE", data / "archive")]
            for p in patches:
                p.start()
            try:
                out = prune.prune(apply=False, today=TODAY, cfg=CFG)
                self.assertEqual((out["roles"], out["signals"], out["events"], out["cutoff"]), (1, 1, 1, "2026-09-09"))
                self.assertEqual(json.loads(files["jobs"].read_text())["a"]["description"], "long ad text")
                self.assertFalse((data / "archive").exists())
                out = prune.prune(apply=True, today=TODAY, cfg=CFG)
                self.assertEqual((out["roles"], out["signals"], out["events"]), (1, 1, 1))
                jobs = json.loads(files["jobs"].read_text())
                self.assertEqual((jobs["a"]["description"], jobs["a"]["pruned"], jobs["b"]["description"]), ("", "2026-11-08", "long ad text"))
                self.assertEqual(list(json.loads(files["signals"].read_text())), ["t"])
                self.assertEqual(list(json.loads(files["events"].read_text())), ["f"])
                self.assertEqual(list(json.loads((data / "archive" / "signals.json").read_text())), ["s"])
                self.assertEqual(json.loads((data / "archive" / "events.json").read_text())["e"], {"closed": "2026-01-01"})
                self.assertEqual(prune.prune(apply=False, today=TODAY, cfg=CFG)["roles"], 0)  # nothing left to do
            finally:
                for p in patches:
                    p.stop()


if __name__ == "__main__":
    unittest.main()
