"""First run must find roles for anyone, anywhere, and must never touch an install already in use."""
import io
import json
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import boards, firstrun, matching, paths, settings, web

READ = {"keywords": ["payments", "roadmap"], "level": "senior"}


def job(title="Senior Designer", location="Berlin", company="Acme", description=""):
    return {"id": f"{company}:{title}:{location}", "title": title, "location": location, "company": company, "description": description, "url": "https://example.com"}


class NewInstall(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.profile, self.jobs = self.dir / "profile.md", self.dir / "jobs.json"
        example = self.dir / "example.md"
        example.write_text("# Example\n")
        for name, value in (("PROFILE", self.profile), ("JOBS", self.jobs), ("EXAMPLE", example)):
            patcher = mock.patch.object(firstrun, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = {}
        patcher = mock.patch.object(firstrun.settings, "load", lambda: self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_saved_means_new(self):
        self.assertTrue(firstrun.needed())

    def test_the_example_profile_still_means_new(self):
        self.profile.write_text("# Example\n")
        self.assertTrue(firstrun.needed())

    def test_a_real_profile_is_enough_on_its_own(self):
        self.profile.write_text("# Me\n\n## CV\nA real CV\n")
        self.assertFalse(firstrun.needed())

    def test_saved_roles_are_enough_on_their_own(self):
        self.jobs.write_text('{"a": {}}')
        self.assertFalse(firstrun.needed())

    def test_a_finished_or_skipped_choice_is_kept(self):
        self.settings = {"setup_skipped": "2026-11-06"}
        self.assertFalse(firstrun.needed())
        self.assertFalse(firstrun.set_up())  # you can still come back to it
        self.settings = {"setup_finished": "2026-11-06"}
        self.assertTrue(firstrun.set_up())

    def test_an_install_in_use_refuses_every_change(self):
        self.profile.write_text("# Me\n")
        firstrun.reset()
        for call in (lambda: firstrun.update({"cv": "x" * 300, "go": True}), firstrun.skip, lambda: firstrun.connect("key")):
            with self.assertRaises(ValueError):
                call()


class NewDefaults(unittest.TestCase):
    def test_a_new_install_inherits_nobodys_search(self):
        text = (paths.ROOT / "config.json").read_text().lower()
        defaults = json.loads(text)
        for word in ("london", "product manager", "£"):
            for key, value in defaults.items():
                if key != "source_company_exclude":
                    self.assertNotIn(word, str(value), key)

    def test_starter_boards_are_public_and_well_formed(self):
        rows = firstrun.starter()
        self.assertGreater(len(rows), 40)
        self.assertLess(sum(r["slug"] == "" for r in rows), 1)
        self.assertTrue(all(r["ats"] in boards.SOURCES for r in rows))
        self.assertEqual(len({r["slug"] for r in rows}), len(rows))


class Reading(unittest.TestCase):
    def test_only_the_expected_shape_gets_through(self):
        d = firstrun.parse_read('Sure! {"titles": ["Designer", 3], "places": "nope", "country_code": "germany", "level": "senior", "extra": 1}')
        self.assertEqual((d["titles"], d["places"], d["country_code"]), (["designer"], [], ""))
        self.assertNotIn("extra", d)

    def test_a_seniority_word_doesnt_narrow_the_search(self):
        d = firstrun.parse_read('{"titles": ["Senior Product Manager", "product manager", "Principal Staff Designer", "lead generation manager"]}')
        self.assertEqual(d["titles"][:2], ["product manager", "designer"])

    def test_a_reply_with_no_answer_is_refused(self):
        with self.assertRaises(ValueError):
            firstrun.parse_read("I can't read this")

    def test_a_berlin_designer_gets_their_own_settings(self):
        read = {"city": "Berlin", "country_code": "de", "currency": "€", "employers": ["Acme"]}
        c = firstrun.changes_for(["product designer"], ["Berlin", "Germany", "Remote"], read)
        self.assertEqual((c["sources_location"], c["sources_country"], c["salary_symbol"]), ("Berlin", "de", "€"))
        self.assertIn("remote (exactly)", c["location_include"])
        groups = settings.groups(c["where_groups"])
        self.assertEqual([g[0] for g in groups], ["Berlin", "Remote", "Germany", "Elsewhere"])
        self.assertEqual(settings.group_of("Berlin, Germany", groups), "Berlin")
        self.assertEqual(settings.group_of("Tokyo", groups), "Elsewhere")
        for key, value in c.items():
            settings.validate(key, value)


class Shortlist(unittest.TestCase):
    def rank(self, j, loved=()):
        return firstrun.rank(j, ["designer"], ["Berlin", "Germany", "Remote"], READ, set(loved))

    def test_the_same_test_as_the_daily_scan(self):
        cfg = firstrun.matcher(["product designer"], ["Berlin", "Remote"])
        self.assertTrue(matching.matches(job("Senior Product Designer", "Berlin, Germany"), cfg))
        self.assertTrue(matching.matches(job("Product Designer", "Remote"), cfg))
        self.assertFalse(matching.matches(job("Product Designer", "Remote - US"), cfg))
        self.assertFalse(matching.matches(job("Product Designer", "Tokyo"), cfg))
        self.assertFalse(matching.matches(job("Product Designer Intern", "Berlin"), cfg))

    def test_your_city_beats_your_country_and_remote(self):
        self.assertGreater(self.rank(job(location="Berlin")), self.rank(job(location="Munich, Germany")))
        self.assertGreater(self.rank(job(location="Munich, Germany")), self.rank(job(location="Remote")))

    def test_your_level_counts(self):
        self.assertGreater(self.rank(job("Senior Designer")), self.rank(job("Junior Designer")))

    def test_a_company_you_love_counts(self):
        self.assertGreater(self.rank(job(), ["acme"]), self.rank(job()))

    def test_your_cv_counts_and_no_company_takes_over(self):
        self.assertGreater(self.rank(job(description="payments roadmap")), self.rank(job()))
        rows = [job(title=str(n)) for n in range(5)] + [job(company="Other")]
        self.assertEqual([r["company"] for r in firstrun.spread(rows, 3)], ["Acme", "Acme", "Other"])


class TimeLimit(unittest.TestCase):
    def tearDown(self):
        web.time_limit(None)

    def test_a_board_that_trickles_is_dropped_at_the_limit(self):
        class Slow(io.BytesIO):
            headers = {}

            def read(self, n=-1):
                time.sleep(.05)
                return b"x"

        with mock.patch("urllib.request.urlopen", lambda *a, **k: Slow()):
            web.time_limit(time.monotonic() + .2)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                web.fetch_bytes("https://example.com/slow")
            self.assertLess(time.monotonic() - started, 1)

    def test_no_retries_once_time_limited(self):
        calls = []

        def down(*a, **k):
            calls.append(1)
            raise OSError("down")

        with mock.patch("urllib.request.urlopen", down):
            web.time_limit(time.monotonic() + 5)
            with self.assertRaises(OSError):
                web.fetch_bytes("https://example.com/down")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
