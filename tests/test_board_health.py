"""Board health: how long a board has gone unread, and what Gus does when a vendor says slow down."""
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from radar import boardhealth, companies, web

NOW = datetime(2026, 11, 16, 8, 5, tzinfo=timezone.utc)


def at(days_ago=0):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


class Unread(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "board_health.json"

    def test_a_failing_board_keeps_the_day_it_was_last_read(self):
        boards = {"Acme": "workday:acme.wd1/jobs", "Bolt": "ashby:bolt"}
        boardhealth.record(boards, {}, at(6), self.path)
        for day in (5, 3, 0):
            boardhealth.record(boards, {"Acme": "robots.txt answered 429"}, at(day), self.path)
        self.assertEqual(boardhealth.unread(NOW, self.path), {"Acme": {"days": 6, "failed": "robots.txt answered 429"}})

    def test_a_board_that_answers_again_is_healthy(self):
        boards = {"Acme": "ashby:acme"}
        boardhealth.record(boards, {"Acme": "HTTP Error 429"}, at(3), self.path)
        boardhealth.record(boards, {}, at(0), self.path)
        self.assertEqual(boardhealth.unread(NOW, self.path), {})

    def test_a_board_never_read_counts_from_its_first_failure(self):
        boards = {"Acme": "ashby:acme"}
        boardhealth.record(boards, {"Acme": "HTTP Error 404"}, at(4), self.path)
        boardhealth.record(boards, {"Acme": "HTTP Error 404"}, at(0), self.path)
        self.assertEqual(boardhealth.unread(NOW, self.path)["Acme"]["days"], 4)

    def test_a_moved_board_starts_afresh_and_a_removed_one_is_forgotten(self):
        boardhealth.record({"Acme": "ashby:acme", "Bolt": "ashby:bolt"}, {}, at(9), self.path)
        boardhealth.record({"Acme": "greenhouse:acme"}, {"Acme": "HTTP Error 404"}, at(0), self.path)
        self.assertEqual(boardhealth.unread(NOW, self.path)["Acme"]["days"], 0)
        self.assertNotIn("Bolt", json.loads(self.path.read_text()))

    def test_the_run_names_boards_unread_for_days_and_not_a_bad_morning(self):
        boards = {"Acme": "ashby:acme", "Bolt": "ashby:bolt", "Cove": "ashby:cove"}
        boardhealth.record(boards, {}, (datetime.now(timezone.utc) - timedelta(days=6)).isoformat(), self.path)
        boardhealth.record(boards, {"Acme": "429"}, (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), self.path)
        boardhealth.record({**boards}, {"Acme": "429", "Cove": "timed out"}, datetime.now(timezone.utc).isoformat(), self.path)
        self.assertEqual(boardhealth.note(self.path), "1 job board not read for 2 days or more (Acme: 6 days)")
        boardhealth.record(boards, {}, datetime.now(timezone.utc).isoformat(), self.path)
        self.assertEqual(boardhealth.note(self.path), "")


class Reply(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def slow_down(url, code=429, headers=None):
    return urllib.error.HTTPError(url, code, "Too Many Requests", headers or {}, None)


class BackingOff(unittest.TestCase):
    """A vendor is every company's board on one supplier. Two tenants of one supplier stand in for it here."""
    A, B = "https://acme.wd1.vendor.example/jobs", "https://bolt.wd5.vendor.example/jobs"

    def setUp(self):
        self.holds = Path(tempfile.mkdtemp()) / "slow_down.json"
        self.asked, self.slept, self.answers = [], [], []
        for patcher in (mock.patch.object(web, "SLOW_DOWN", self.holds), mock.patch.dict(web._hosts, clear=True),
                        mock.patch.dict(web._strikes, clear=True), mock.patch.object(web, "_cooled", set()),
                        mock.patch.object(web.time, "sleep", self.slept.append), mock.patch("urllib.request.urlopen", self.urlopen)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def urlopen(self, req, timeout=30):
        self.asked.append(req.full_url)
        answer = self.answers.pop(0) if self.answers else b"ok"
        if isinstance(answer, Exception):
            raise answer
        return Reply(answer)

    def refuse(self, url, times=1):
        for _ in range(times):
            self.answers.append(slow_down(url))
            with self.assertRaises(urllib.error.HTTPError):
                web.fetch_bytes(url)

    def test_companies_on_one_vendor_share_a_name(self):
        self.assertEqual(web.vendor("acme.wd3.myworkdayjobs.com"), web.vendor("bolt.wd5.myworkdayjobs.com"))
        self.assertEqual(web.vendor("jobs.bbc.co.uk"), "bbc.co.uk")
        self.assertNotEqual(web.vendor("api.ashbyhq.com"), web.vendor("boards-api.greenhouse.io"))
        self.assertEqual(web.vendor("127.0.0.1:8000"), "127.0.0.1")

    def test_a_few_refusals_among_answers_change_nothing(self):
        self.refuse(self.A, 2)
        web.fetch_bytes(self.B)
        self.refuse(self.A, 2)
        self.assertFalse(self.holds.exists())
        self.assertLess(max(self.slept, default=0), web.COOL_OFF / 2, "no cool-off either")

    def test_refusals_in_a_row_first_cool_off_then_stop_asking_the_vendor(self):
        self.refuse(self.A, web.STRIKES)
        self.assertFalse(self.holds.exists(), "the first time, the vendor is only left alone for a minute")
        self.refuse(self.B, 1)
        self.assertGreater(max(self.slept), web.COOL_OFF - 5)
        self.slept.clear()
        self.refuse(self.B, web.STRIKES - 1)
        self.assertAlmostEqual(self.slept[-1] - self.slept[-2], web.SLOWER_PAUSE, delta=0.5, msg="after the cool-off, Gus asks the vendor more slowly")
        hold = json.loads(self.holds.read_text())["vendor.example"]
        self.assertEqual(hold["times"], 1)
        asked = len(self.asked)
        with self.assertRaises(web.SlowDown) as stopped:
            web.fetch_bytes(self.A)
        self.assertIn("told Gus to slow down", str(stopped.exception))
        self.assertEqual(len(self.asked), asked, "a vendor on hold isn't asked at all")
        self.assertEqual(web.fetch_bytes("https://api.other.example/jobs"), b"ok", "other vendors carry on")

    def hold_that_ran_out(self, times=1):
        self.holds.write_text(json.dumps({"vendor.example": {"until": "2026-01-01T00:00:00+00:00", "times": times, "said": "2026-01-01T00:00:00+00:00"}}))

    def test_a_vendor_that_still_says_no_after_its_hold_waits_twice_as_long(self):
        self.hold_that_ran_out(times=2)
        before = datetime.now(timezone.utc)
        self.refuse(self.A, 1)
        hold = json.loads(self.holds.read_text())["vendor.example"]
        self.assertEqual(hold["times"], 3)
        waits = (datetime.fromisoformat(hold["until"]) - before).total_seconds()
        self.assertAlmostEqual(waits, web.FIRST_HOLD * 4, delta=5)

    def test_no_hold_is_as_long_as_a_day_so_the_daily_run_always_asks(self):
        self.hold_that_ran_out(times=9)
        self.refuse(self.A, 1)
        hold = json.loads(self.holds.read_text())["vendor.example"]
        self.assertLess((datetime.fromisoformat(hold["until"]) - datetime.now(timezone.utc)).total_seconds(), 24 * 3600)

    def test_an_answer_ends_the_hold_for_good(self):
        self.hold_that_ran_out()
        self.assertEqual(web.fetch_bytes(self.A), b"ok")
        self.assertEqual(json.loads(self.holds.read_text()), {})

    def test_a_short_wait_the_vendor_asks_for_is_waited_once(self):
        self.answers += [slow_down(self.A, headers={"Retry-After": "5"}), b"jobs"]
        self.assertEqual(web.fetch_bytes(self.A), b"jobs")
        self.assertIn(5, self.slept)
        self.answers += [slow_down(self.A, headers={"Retry-After": "3600"})]
        asked = len(self.asked)
        with self.assertRaises(urllib.error.HTTPError):
            web.fetch_bytes(self.A)
        self.assertEqual(len(self.asked), asked + 1, "a long wait isn't sat through, and the request isn't repeated")

    def test_a_bot_check_counts_as_slow_down_and_a_plain_refusal_does_not(self):
        self.assertTrue(web.told_to_slow_down(slow_down(self.A, 403, {"cf-mitigated": "challenge"})))
        self.assertFalse(web.told_to_slow_down(slow_down(self.A, 403)))
        self.assertFalse(web.told_to_slow_down(slow_down(self.A, 404)))


class NoRecheck(unittest.TestCase):
    def test_a_board_told_to_slow_down_is_not_sent_back_to_be_checked_again(self):
        made_up = {"Acme": {"status": "watched", "assessed": "2999-01-01", "board_broken": "workday:acme.wd1/jobs: robots.txt answered 429"},
                   "Bolt": {"status": "watched", "assessed": "2999-01-01", "board_broken": "ashby:bolt: HTTP Error 404: Not Found"},
                   "Cove": {"status": "watched", "assessed": "2999-01-01", "board_broken": "Not asked: vendor.example told Gus to slow down. Gus asks it again after 10:05 on 16 Nov"}}
        with mock.patch.object(companies, "load", lambda: made_up), mock.patch.object(companies, "no_board", lambda c: []):
            self.assertEqual(companies.due()["broken_boards"], ["Bolt"])


if __name__ == "__main__":
    unittest.main()
