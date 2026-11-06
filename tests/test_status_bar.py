"""The status bar's agent time has to be the newest run, whatever each run's file is called."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import server


class LastAgentRun(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        (self.data / "runs").mkdir()
        patcher = mock.patch.object(server, "DATA", self.data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_newest_time_wins_over_the_last_name(self):
        for name in ("2026-11-05T23-10-02Z.md", "discover-2026-11-05T22-50-24Z-report.md", "watchlist-2026-11-04T08-27-42Z.md"):
            (self.data / "runs" / name).write_text("run")
        self.assertEqual(server.last_agent_run(), "2026-11-05T23:10:02Z")

    def test_no_runs_yet(self):
        self.assertIsNone(server.last_agent_run())
        (self.data / "runs").rmdir()
        self.assertIsNone(server.last_agent_run())


if __name__ == "__main__":
    unittest.main()
