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

    def hold_that_ran_out(self, times=1, days_ago=1):
        said = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
        self.holds.write_text(json.dumps({"vendor.example": {"until": said, "times": times, "said": said}}))

    def hold(self):
        return json.loads(self.holds.read_text()).get("vendor.example")

    def test_a_vendor_held_before_gets_one_slow_retry_before_a_longer_hold(self):
        self.hold_that_ran_out(times=1)
        ran_out = self.hold()["until"]
        self.refuse(self.A, 1)
        self.assertEqual(self.hold()["until"], ran_out, "one refusal the morning after doesn't cost the day")
        self.refuse(self.B, 1)
        self.assertGreater(max(self.slept), web.COOL_OFF - 5, "the second try comes a minute later")
        self.assertEqual(self.hold()["times"], 2)

    def test_a_vendor_that_still_says_no_after_its_hold_waits_twice_as_long(self):
        self.hold_that_ran_out(times=2)
        before = datetime.now(timezone.utc)
        self.refuse(self.A, 2)
        self.assertEqual(self.hold()["times"], 3)
        waits = (datetime.fromisoformat(self.hold()["until"]) - before).total_seconds()
        self.assertAlmostEqual(waits, web.FIRST_HOLD * 4, delta=5)

    def test_no_hold_is_as_long_as_a_day_so_the_daily_run_always_asks(self):
        self.hold_that_ran_out(times=9)
        self.refuse(self.A, 2)
        self.assertLess((datetime.fromisoformat(self.hold()["until"]) - datetime.now(timezone.utc)).total_seconds(), 24 * 3600)

    def test_an_answer_ends_the_hold_and_the_next_one_starts_short_again(self):
        self.hold_that_ran_out(times=3)
        self.assertEqual(web.fetch_bytes(self.A), b"ok")
        self.assertEqual(self.hold()["times"], 0)
        self.assertNotIn("until", self.hold())
        self.assertEqual(web.hold_ended(), "", "nothing is waiting on this vendor any more")

    def test_a_vendor_that_said_slow_down_this_week_is_asked_slowly_even_once_it_answers(self):
        self.hold_that_ran_out(days_ago=web.CAREFUL_DAYS - 1)
        web.fetch_bytes(self.A)
        web.fetch_bytes(self.B)
        web.fetch_bytes(self.A)
        self.assertAlmostEqual(self.slept[-1] - self.slept[-2], web.SLOWER_PAUSE, delta=0.5)
        self.assertGreaterEqual(web.SLOWER_PAUSE, 10)

    def test_a_vendor_that_has_answered_for_a_week_goes_back_to_the_usual_pace(self):
        self.hold_that_ran_out(days_ago=web.CAREFUL_DAYS + 1)
        web.fetch_bytes(self.A)
        web.fetch_bytes(self.B)
        web.fetch_bytes(self.A)
        self.assertAlmostEqual(self.slept[-1] - self.slept[-2], web.HOST_PAUSE, delta=0.5)
        self.assertIsNone(self.hold(), "and it's forgotten")

    def test_a_vendor_whose_boards_take_many_requests_is_always_asked_gently(self):
        a, b = "https://acme.wd1.myworkdayjobs.com/jobs", "https://bolt.wd5.myworkdayjobs.com/jobs"
        self.assertTrue(web.unhurried("myworkdayjobs.com"), "though it has never said slow down")
        self.assertFalse(web.unhurried("vendor.example"))
        web.fetch_bytes(a)
        web.fetch_bytes(b)
        self.assertAlmostEqual(self.slept[-1], web.GENTLE["myworkdayjobs.com"], delta=0.5)
        self.assertGreaterEqual(web.GENTLE["myworkdayjobs.com"], 30)

    def test_sites_that_turned_gus_away_at_the_usual_pace_are_asked_slowly_for_good(self):
        self.assertGreaterEqual(web.pause("workable.com"), 10, "all twelve boards read at this pace after refusing at 1.5 seconds")
        self.assertGreaterEqual(web.pause("microsoft.com"), 60, "it refused the third request at 10 seconds apart")
        self.assertEqual(web.vendor("apply.careers.microsoft.com"), "microsoft.com")
        self.assertEqual(web.vendor("apply.workable.com"), "workable.com")

    def test_the_hold_keeps_what_the_vendor_said(self):
        self.refuse(self.A, web.STRIKES)
        self.answers += [slow_down(self.A, headers={"Retry-After": "900"})] * web.STRIKES
        for _ in range(web.STRIKES):
            with self.assertRaises(urllib.error.HTTPError):
                web.fetch_bytes(self.A)
        self.assertEqual(self.hold()["why"], "too many requests (429)")
        self.assertEqual(self.hold()["asked_for"], 900)
        with self.assertRaises(web.SlowDown) as stopped:
            web.fetch_bytes(self.B)
        self.assertIn("too many requests (429)", str(stopped.exception))
        self.assertEqual(web.why(slow_down(self.A, 403, {"cf-mitigated": "challenge"})), "a bot check (403)")

    def test_a_hold_that_has_run_out_says_when_until_the_vendor_answers(self):
        self.assertEqual(web.hold_ended(), "")
        self.hold_that_ran_out()
        self.assertEqual(web.hold_ended(), self.hold()["until"])
        later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        self.holds.write_text(json.dumps({"vendor.example": {"until": later, "times": 1, "said": later}}))
        self.assertEqual(web.hold_ended(), "", "still on hold")

    def test_boards_that_were_held_back_are_told_from_ones_that_broke(self):
        self.assertTrue(web.held_back("workable:acme: Not asked: workable.com told Gus to slow down (too many requests (429)). Gus asks it again after 12:02 on 17 Nov"))
        self.assertTrue(web.held_back("workable:acme: HTTP Error 429: Too Many Requests"))
        self.assertFalse(web.held_back("ashby:bolt: HTTP Error 404: Not Found"))
        self.assertFalse(web.held_back("Microsoft's careers site asked Gus to slow down. The next daily read tries again"))

    def test_a_short_wait_the_vendor_asks_for_is_waited_once(self):
        self.answers += [slow_down(self.A, headers={"Retry-After": "5"}), b"jobs"]
        self.assertEqual(web.fetch_bytes(self.A), b"jobs")
        self.assertIn(5, self.slept)
        self.answers += [slow_down(self.A, headers={"Retry-After": "3600"})]
        asked = len(self.asked)
        with self.assertRaises(urllib.error.HTTPError):
            web.fetch_bytes(self.A)
        self.assertEqual(len(self.asked), asked + 1, "a long wait isn't sat through, and the request isn't repeated")

    def test_a_bot_check_that_answers_429_is_not_taken_for_too_many_requests(self):
        check = {"cf-mitigated": "challenge"}
        self.assertEqual(web.why(slow_down(self.A, 429, check)), "a bot check (429)")
        self.answers.append(slow_down(self.A, 429, check))
        with self.assertRaises(urllib.error.HTTPError) as said:
            web.fetch_bytes(self.A)
        self.assertIn("bot check", str(said.exception))
        self.assertFalse(web.held_back(f"workday:acme: {said.exception}"), "waiting doesn't cure a bot check")
        for _ in range(2 * web.STRIKES - 1):
            self.answers.append(slow_down(self.A, 429, check))
            with self.assertRaises(urllib.error.HTTPError):
                web.fetch_bytes(self.A)
        self.assertEqual(self.hold()["why"], "a bot check (429)")
        with self.assertRaises(web.SlowDown) as stopped:
            web.fetch_bytes(self.B)
        self.assertIn("a bot check (429)", str(stopped.exception))
        self.assertNotIn("slow down", str(stopped.exception))
        self.assertFalse(web.held_back(f"workday:bolt: {stopped.exception}"), "so the board isn't read again when the hold ends")

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


class ThreeBoards(unittest.TestCase):
    """A watchlist of three made-up boards, two of them on a vendor that is holding Gus back."""

    def setUp(self):
        import csv
        from radar import boards, matching, scan
        self.dir = Path(tempfile.mkdtemp())
        with (self.dir / "watchlist.csv").open("w", newline="") as f:
            csv.writer(f).writerows([["company", "ats", "slug", "sector", "only"], ["Acme", "fake", "acme", "ai", ""],
                                     ["Bolt", "fake", "bolt", "ai", ""], ["Cove", "fake", "cove", "ai", ""]])
        self.asked, self.refusing = [], {"Acme", "Cove"}

        def board(company, slug):
            self.asked.append(company)
            if company in self.refusing:
                raise web.SlowDown("Not asked: vendor.example told Gus to slow down. Gus asks it again after 12:02 on 17 Nov")
            return [{"id": f"fake:{slug}:1", "company": company, "title": "Product Manager"}]

        for patcher in (mock.patch.dict(boards.SOURCES, fake=board, clear=True), mock.patch.object(scan, "watchlist_path", lambda: self.dir / "watchlist.csv"),
                        mock.patch.object(matching, "load_config", lambda: {"source_hn": False}), mock.patch.object(boards, "save_looked_up", lambda a: None),
                        mock.patch.object(scan, "DATA", self.dir)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.scan = scan

    def errors(self):
        return json.loads((self.dir / "board_errors.json").read_text())


class CatchUp(ThreeBoards):
    """Boards a vendor held back in the morning are read again the same day, once its hold has run out."""

    def test_only_the_boards_held_back_are_read_again_and_the_rest_keep_their_record(self):
        self.scan.fetch_all()
        self.assertEqual(self.scan.waiting(), {"Acme", "Cove"})
        health = json.loads((self.dir / "board_health.json").read_text())
        self.asked.clear()
        self.refusing = {"Cove"}
        jobs, _ = self.scan.fetch_all(self.scan.waiting())
        self.assertEqual(sorted(self.asked), ["Acme", "Cove"], "Bolt answered this morning, so it isn't asked twice")
        self.assertEqual([j["company"] for j in jobs], ["Acme"])
        self.assertEqual(list(self.errors()), ["Cove"], "Acme is read now; Cove is still waiting")
        after = json.loads((self.dir / "board_health.json").read_text())
        self.assertEqual(after["Bolt"], health["Bolt"], "a board that wasn't asked keeps when it was last read")
        self.assertNotIn("failed", after["Acme"])

    def test_a_board_that_broke_is_not_one_that_is_waiting(self):
        (self.dir / "board_errors.json").write_text(json.dumps({"Bolt": "ashby:bolt: HTTP Error 404: Not Found"}))
        self.assertEqual(self.scan.waiting(), set())


class NewBoardsOnly(ThreeBoards):
    """A run's later scans are for boards added during it: the morning's scan has already asked every other one."""

    def setUp(self):
        super().setUp()
        self.refusing = set()

    def add(self, *row):
        import csv
        with (self.dir / "watchlist.csv").open("a", newline="") as f:
            csv.writer(f).writerow(row)

    def test_a_later_scan_asks_only_the_boards_no_scan_has_asked(self):
        self.scan.fetch_all()
        self.add("Dune", "fake", "dune", "ai", "")
        self.asked.clear()
        self.assertEqual(self.scan.unasked(), {"Dune"})
        self.scan.fetch_all(self.scan.unasked())
        self.assertEqual(self.asked, ["Dune"], "the three read this morning aren't asked a second time")
        self.assertEqual(self.scan.unasked(), set())

    def test_a_board_that_moved_counts_as_new(self):
        self.scan.fetch_all()
        rows = (self.dir / "watchlist.csv").read_text().replace("Bolt,fake,bolt", "Bolt,fake,bolt-moved")
        (self.dir / "watchlist.csv").write_text(rows)
        self.assertEqual(self.scan.unasked(), {"Bolt"})

    def test_with_nothing_new_no_board_is_asked(self):
        self.scan.fetch_all()
        self.asked.clear()
        with mock.patch.object(self.scan.sys, "argv", ["scan", "--new"]), mock.patch("builtins.print") as said:
            self.scan.main()
        self.assertEqual((self.asked, said.call_args[0][0]), ([], "No new boards to read"))

    def test_the_run_and_the_agent_ask_for_new_boards_only(self):
        from radar import resolve, service
        ran = []
        with mock.patch.object(resolve, "auto", lambda: {"added": ["Dune"], "guesses": [], "restored": []}), \
             mock.patch.object(service.subprocess, "run", lambda cmd, **kw: ran.append(cmd[2:])):
            service._boards()
        self.assertIn(["radar.scan", "--new"], ran)
        self.assertIn("python3 -m radar.scan --new", (Path(__file__).parents[1] / "prompts" / "discover.md").read_text())


class ScanLog(ThreeBoards):
    """Each scan leaves a line saying when it ran, what kind it was and which boards it couldn't read."""

    def test_a_scan_writes_what_it_asked_and_what_went_wrong(self):
        with mock.patch.object(self.scan, "JOBS", self.dir / "jobs.json"), mock.patch.object(self.scan.sys, "argv", ["scan"]), \
             mock.patch.object(self.scan.web, "remember_robots", lambda p: None), mock.patch.object(self.scan.web, "keep_robots", lambda p: None), \
             mock.patch.object(self.scan.matching, "matches", lambda *a: False), mock.patch("builtins.print"):
            self.scan.main()
        log = (self.dir / "scan.log").read_text()
        self.assertRegex(log.splitlines()[0], r"^\d{4}-\d\d-\d\dT[\d:]+\+00:00 every board: 1 of 3 read in \d+s, 0 new roles$")
        self.assertIn("  Acme (fake:acme: Not asked: vendor.example told Gus to slow down", log)

    def test_the_log_doesnt_grow_for_ever(self):
        (self.dir / "scan.log").write_text("old\n" * (self.scan.LOG_LINES + 50))
        self.scan.log("new")
        lines = (self.dir / "scan.log").read_text().splitlines()
        self.assertEqual((len(lines), lines[-1]), (self.scan.LOG_LINES, "new"))


class SlowLane(unittest.TestCase):
    """Boards on a vendor Gus asks slowly are read one after another in a slot of their own, so the other boards don't wait on them."""

    def setUp(self):
        import csv
        import threading
        from radar import boards, matching, scan
        self.dir = Path(tempfile.mkdtemp())
        slow, quick = [f"Slow{n}" for n in range(8)], [f"Quick{n}" for n in range(8)]
        with (self.dir / "watchlist.csv").open("w", newline="") as f:
            csv.writer(f).writerows([["company", "ats", "slug", "sector", "only"], *([c, "slow", c.lower(), "ai", ""] for c in slow),
                                     *([c, "quick", c.lower(), "ai", ""] for c in quick)])
        self.said = []
        self.quick_done, self.at_once, self.most_at_once, self.stalled, lock = threading.Event(), 0, 0, False, threading.Lock()
        quick_left = [len(quick)]

        def slow_board(company, slug):
            with lock:
                self.at_once += 1
                self.most_at_once = max(self.most_at_once, self.at_once)
            self.stalled = self.stalled or not self.quick_done.wait(5)  # a slow vendor: its answer comes after everyone else's
            with lock:
                self.at_once -= 1
            return [{"id": f"slow:{slug}:1", "company": company, "title": "Product Manager"}]

        def quick_board(company, slug):
            with lock:
                quick_left[0] -= 1
                if not quick_left[0]:
                    self.quick_done.set()
            return [{"id": f"quick:{slug}:1", "company": company, "title": "Product Manager"}]

        for patcher in (mock.patch.dict(boards.SOURCES, slow=slow_board, quick=quick_board, clear=True),
                        mock.patch.dict(boards.VENDORS, slow="vendor.example", clear=True),
                        mock.patch.object(web, "unhurried", lambda who: who == "vendor.example"),
                        mock.patch.object(scan, "watchlist_path", lambda: self.dir / "watchlist.csv"),
                        mock.patch.object(matching, "load_config", lambda: {"source_hn": False}),
                        mock.patch.object(boards, "save_looked_up", lambda a: None), mock.patch.object(scan, "DATA", self.dir),
                        mock.patch.object(scan.doing, "say", self.said.append)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.scan = scan

    def test_the_progress_says_when_only_slow_sites_are_left(self):
        self.scan.fetch_all()
        self.assertEqual(len(self.said), 16, "one line for each board as it finishes, slow ones included")
        self.assertNotIn("slowly", " ".join(self.said[:7]))
        self.assertIn("8 of 16 done", self.said[7])
        self.assertIn("The other 8 are on job sites Gus asks slowly", self.said[7])
        self.assertIn("The other 1 is on a job site Gus asks slowly", self.said[14])
        self.assertNotIn("slowly", self.said[15])

    def test_slow_boards_go_one_at_a_time_and_never_hold_up_the_rest(self):
        jobs, errors = self.scan.fetch_all()
        self.assertFalse(self.stalled, "the quick boards were all read while the slow vendor was still answering its first")
        self.assertEqual(self.most_at_once, 1, "one board at a time on the slow vendor")
        self.assertEqual((len(jobs), errors), (16, []), "and every board is read")
        self.assertEqual(json.loads((self.dir / "board_errors.json").read_text()), {})


class CatchUpTime(unittest.TestCase):
    ENDED = "2026-11-17T12:02:04+00:00"

    def look(self, ended=ENDED, waiting=("Acme",), state=None, cfg=None):
        from radar import scan, service
        started, state = [], dict(state or {})
        with mock.patch.object(service, "load_config", return_value={"scheduler_enabled": True, "run_time": "", **(cfg or {})}), \
             mock.patch.object(service, "_schedule_state", return_value=state), mock.patch.object(service, "_save_schedule_state"), \
             mock.patch.object(service, "start", side_effect=lambda job: started.append(job) or True), \
             mock.patch.object(web, "hold_ended", return_value=ended), mock.patch.object(scan, "waiting", return_value=set(waiting)):
            service.tick(False, datetime(2026, 11, 17, 12, 3))
        return started, state

    def test_when_a_hold_runs_out_the_boards_it_held_back_are_read_once(self):
        started, state = self.look()
        self.assertEqual(started, ["catchup"])
        self.assertEqual(self.look(state=state)[0], [], "once for each hold, not every minute")

    def test_nothing_to_catch_up_on(self):
        self.assertEqual(self.look(ended="")[0], [], "no hold has run out")
        self.assertEqual(self.look(waiting=())[0], [], "no board is waiting on one")
        self.assertEqual(self.look(cfg={"scheduler_enabled": False})[0], [], "Gus only runs by itself when the scheduler is on")


if __name__ == "__main__":
    unittest.main()
