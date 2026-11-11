"""When the Claude sign-in Gus uses expires, Gus says so once a day and in the cockpit, instead of failing quietly."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import ai

EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"


def claude(returncode=0, result="ok", is_error=False):
    body = json.dumps({"result": result, "is_error": is_error, "total_cost_usd": 0, "usage": {}})
    return mock.Mock(returncode=returncode, stdout=body, stderr="")


class LoginExpiry(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.flag = Path(folder.name) / "login_expired"
        patches = [mock.patch.object(ai, "EXPIRED_FLAG", self.flag),
                   mock.patch.object(ai, "load_config", lambda: {"ai_provider": "claude_subscription", "ai_daily_budget_usd": 100}),
                   mock.patch.object(ai.usage, "record", lambda **k: None),
                   mock.patch.object(ai.usage, "today_cost", lambda: 0),
                   mock.patch("radar.notify.cockpit_url", lambda: "https://gus.example")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.notify = mock.patch("radar.notify.notify").start()  # a test must never send a real push
        self.addCleanup(mock.patch.stopall)

    def run_claude(self, reply):
        with mock.patch.object(subprocess, "run", return_value=reply):
            return ai.run("hello", task="scoring")

    def test_an_expired_login_is_recorded_and_pushed_once_a_day(self):
        with self.assertRaises(ai.LoginExpired) as caught:
            self.run_claude(claude(returncode=1, result=EXPIRED))
        self.assertIn("Settings → AI", str(caught.exception))
        self.assertNotIn("/login", str(caught.exception))
        self.assertTrue(self.flag.exists())
        self.assertEqual(self.notify.call_count, 1)
        title, _, message = self.notify.call_args.args[:3]
        self.assertEqual(title, "Claude sign-in expired")
        self.assertIn("Settings → AI", message)
        self.assertEqual(self.notify.call_args.kwargs["click"], "https://gus.example/#settings/ai")
        with self.assertRaises(ai.LoginExpired):  # the next failed run the same day stays quiet
            self.run_claude(claude(returncode=1, result=EXPIRED))
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(json.loads(self.flag.read_text())["notified"], str(ai.date.today()))

    def test_the_push_comes_again_the_next_day(self):
        self.flag.write_text(json.dumps({"since": "2026-11-08T09:00:00+00:00", "notified": "2026-11-08"}))
        with self.assertRaises(ai.LoginExpired):
            self.run_claude(claude(returncode=1, result=EXPIRED))
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(json.loads(self.flag.read_text())["since"], "2026-11-08T09:00:00+00:00")  # the first failure's time is kept

    def test_a_working_run_clears_it(self):
        self.flag.write_text(json.dumps({"since": "x", "notified": "2026-11-08"}))
        self.assertEqual(self.run_claude(claude(result="fine")), "fine")
        self.assertFalse(self.flag.exists())
        self.assertIsNone(ai.login_expired())

    def test_other_failures_are_not_mistaken_for_it(self):
        with self.assertRaises(RuntimeError) as caught:
            self.run_claude(claude(returncode=1, result="Request timed out"))
        self.assertNotIsInstance(caught.exception, ai.LoginExpired)
        self.assertNotIn("/login", str(caught.exception))
        self.assertFalse(self.flag.exists())
        self.notify.assert_not_called()

    def test_status_says_so_even_when_claude_still_claims_a_login(self):
        self.flag.write_text(json.dumps({"since": "x", "notified": "2026-11-08"}))
        logged_in = mock.Mock(returncode=0, stdout=json.dumps({"loggedIn": True}), stderr="")
        with mock.patch.object(subprocess, "run", return_value=logged_in), mock.patch.object(ai.shutil, "which", lambda n: "/usr/bin/claude"):
            s = ai.status()
        self.assertFalse(s["ok"])
        self.assertIn("expired", s["reply"])
        self.assertIn("Settings → AI", s["reply"])

    def test_signing_in_again_clears_it(self):
        self.flag.write_text(json.dumps({"since": "x", "notified": "2026-11-08"}))
        proc = mock.Mock(returncode=0)
        proc.poll.return_value = None
        with mock.patch.object(ai, "_login", proc), mock.patch.object(ai, "status", lambda: {"ok": True, "reply": ""}):
            self.assertTrue(ai.login_finish("code")["ok"])
        self.assertFalse(self.flag.exists())

    def test_the_full_run_stops_its_ai_steps(self):
        from radar import service
        calls = []

        def step(name):
            def go():
                if name == "discover":
                    raise ai.LoginExpired("Claude sign-in has expired")
                calls.append(name)
                return True, ""
            return go
        with mock.patch.multiple(service, _check_roles=step("scan"), _agent_step=step, _boards=step("boards"), _run_modules=lambda *a: step("score")(),
                                 _emails_waiting=lambda: 0, _moves_this_week=lambda: 0, _watchlist_due=lambda c, s: True,
                                 _counts=lambda: {"roles": 0, "strong": 0}, _schedule_state=lambda: {}, _save_schedule_state=lambda s: None, load_config=lambda: {}):
            service.progress.clear()
            ok, summary = service.full_run()
        self.assertFalse(ok)
        self.assertIn("sign-in has expired", summary)
        self.assertEqual(calls, ["scan"])


if __name__ == "__main__":
    unittest.main()
