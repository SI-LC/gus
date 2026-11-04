"""python3 -m unittest discover -s tests"""
import unittest
from datetime import datetime

from radar.network import distance, paths_for
from radar.service import due

CFG = {"scan_times": "08:00, 13:00, 18:00", "agent_times_weekdays": "08:00", "agent_times_weekends": "",
       "watchlist_every_days": 3, "watchlist_time": "07:30", "moves_day": "Mon", "moves_time": "08:30"}
DONE = {"watchlist": "2026-11-04", "moves": "2026-11-02"}


def at(day, h, m=0):
    return datetime(2026, 11, day, h, m)


class Schedule(unittest.TestCase):
    def test_nothing_before_first_slot(self):
        self.assertEqual(due(at(4, 7), CFG, DONE), [])

    def test_morning_slot_has_scan_and_discovery(self):
        self.assertEqual(due(at(4, 8), CFG, DONE), ["scan", "agent"])

    def test_each_slot_runs_once(self):
        s = {**DONE, "scan": "2026-11-04 08:00", "agent": "2026-11-04 08:00"}
        self.assertEqual(due(at(4, 8, 30), CFG, s), [])
        self.assertEqual(due(at(4, 13), CFG, s), ["scan"])

    def test_no_discovery_at_weekends(self):
        self.assertEqual(due(at(7, 8), CFG, {"watchlist": "2026-11-07", "moves": "2026-11-02"}), ["scan"])

    def test_moves_on_monday_after_morning_run(self):
        s = {"watchlist": "2026-11-09", "moves": "2026-11-02", "scan": "2026-11-09 08:00", "agent": "2026-11-09 08:00"}
        self.assertEqual(due(at(9, 8), CFG, s), [])
        self.assertEqual(due(at(9, 8, 30), CFG, s), ["moves"])


class Distance(unittest.TestCase):
    CONNS = [{"name": "Ada Example", "url": "u", "company": "Acme Ltd", "position": "PM"}]

    def label(self, person, conns=()):
        return distance(paths_for(person, list(conns), {}, ["Globex"]))

    def test_estimates_are_marked(self):
        d = self.label({"name": "Pat", "company": "Acme"}, self.CONNS)
        self.assertEqual((d["label"], d["certain"]), ("~2nd", False))

    def test_confirmed_beats_estimate(self):
        d = self.label({"name": "Pat", "company": "Acme", "degree": 3}, self.CONNS)
        self.assertEqual((d["label"], d["certain"]), ("3rd+", True))

    def test_reply_means_in_touch(self):
        self.assertEqual(self.label({"name": "Pat", "company": "Acme", "stage": "replied"})["label"], "In touch")

    def test_no_route_is_cold(self):
        self.assertEqual(self.label({"name": "Pat", "company": "Other"})["label"], "3rd+")


if __name__ == "__main__":
    unittest.main()
