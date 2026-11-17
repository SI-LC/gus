"""The demo (evals/demo.py): a made-up search that still matches docs/objects.md, fills every screen and names no one real."""
import json
import re
import unittest

from evals import demo
from radar import schema
from radar.schema import JOB_STAGES, OBJECTS


class Demo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = demo.build()
        cls.addClassCleanup(cls.world.close)
        cls.data = cls.world.app / "data"

    def read(self, name):
        return json.loads((self.data / f"{name}.json").read_text())

    def test_matches_the_objects_page(self):
        self.assertEqual(schema.check(self.data), {})

    def test_has_every_object(self):
        for name, (file, *_) in OBJECTS.items():
            self.assertTrue(json.loads((self.data / file).read_text()), f"the demo has no {name}: add some to evals/demo.py")

    def test_has_a_role_at_every_stage(self):
        self.assertEqual({j["stage"] for j in self.read("jobs").values()}, set(JOB_STAGES))

    def test_this_weeks_moves_are_ones_the_planner_would_keep(self):
        from radar.accounts import validate, week_id
        companies, people, events, moves = (self.read(n) for n in ("companies", "people", "events", "moves"))
        todo = [m for m in moves.values() if m["week"] == week_id() and m["status"] == "todo"]
        self.assertTrue(todo)
        for m in todo:
            self.assertEqual(validate(m, companies, people, events, {}), [], m["id"])

    def test_points_nowhere_real(self):
        """Every link is on example.com or a .example site, apart from the board Gus guessed for a made-up company."""
        text = "".join(p.read_text() for p in self.data.glob("*.json*"))
        hosts = set(re.findall(r'https?://([^/"\\\s]+)', text))
        self.assertEqual({h for h in hosts if h != "example.com" and not h.endswith(".example")}, {"job-boards.greenhouse.io"})

    def test_cockpit_says_its_a_demo(self):
        self.world.start()
        self.addCleanup(self.world.stop)
        state = self.world.get("/api/state")
        self.assertTrue(state["demo"])
        self.assertFalse(state["setup"]["needed"])
        self.assertTrue(state["follow_ups"] and state["meetings"] and state["board_guesses"])


if __name__ == "__main__":
    unittest.main()
