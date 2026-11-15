"""What a phone notification says: its button names what it opens, and what was held overnight reads as a list."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import notify as N


class Push(unittest.TestCase):
    def setUp(self):
        self.sent = []
        for p in (mock.patch.object(N, "_topic", lambda: "topic"), mock.patch.object(N, "cockpit_url", lambda: ""),
                  mock.patch.object(N, "quiet_now", lambda spec=None: False), mock.patch.object(N.platform, "system", lambda: "Linux"),
                  mock.patch.object(N, "_cfg", lambda: {}),
                  mock.patch.object(N.urllib.request, "urlopen", lambda req, timeout: self.sent.append(req))):
            p.start()
            self.addCleanup(p.stop)

    def headers(self):
        return {k.lower(): (v.decode() if isinstance(v, bytes) else v) for k, v in self.sent[-1].headers.items()}

    def test_a_role_push_opens_the_ad(self):
        N.notify("Acme", "Senior PM", "Scores 8 of 10. Fits.", "https://acme.example/jobs/1")
        self.assertEqual(self.headers()["title"], "Acme: Senior PM")
        self.assertEqual(self.headers()["actions"], "view, Open the ad, https://acme.example/jobs/1")

    def test_a_news_push_says_it_opens_the_news(self):
        N.notify("Acme posted about hiring", "", "We're growing", "https://news.example/1", action="Read the news")
        self.assertEqual(self.headers()["actions"], "view, Read the news, https://news.example/1")

    def test_what_was_held_overnight_reads_as_a_list(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(N, "HELD", Path(d) / "held.json"):
            N.HELD.write_text(json.dumps([["Acme", "Senior PM", "x", "", ""], ["Beta posted about hiring", "", "y", "", ""]]))
            N.release_held()
        self.assertEqual(self.headers()["title"], "2 things came in overnight")
        self.assertEqual(self.sent[-1].data.decode(), "Acme: Senior PM; Beta posted about hiring")


if __name__ == "__main__":
    unittest.main()
