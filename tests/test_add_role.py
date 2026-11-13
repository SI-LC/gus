"""A role added by hand (/api/job/add): one a recruiter brought, with no public ad. It's yours, so no scan closes it and no score skips it."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import events as E, jobs as J, resolve, schema, server, triage
from radar.scan import mark_closed

NOW = "2026-11-11T10:00:00+00:00"


class AddRole(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.dir, self.jobs, self.companies = d, d / "jobs.json", d / "companies.json"
        self.companies.write_text(json.dumps({"Acme": {"status": "watched", "tier": "A"}}))
        self.watchlist = d / "watchlist.csv"
        self.watchlist.write_text("company,ats,slug,sector,only\nAcme,greenhouse,acme,,\n")
        self.lookup = mock.Mock(return_value=None)  # the board search, which would otherwise go to the internet
        for p in (mock.patch.object(server, "JOBS", self.jobs), mock.patch.object(server, "COMPANIES", self.companies),
                  mock.patch.object(resolve, "resolve", self.lookup), mock.patch("radar.resolve.watchlist_path", lambda: self.watchlist)):
            p.start()
            self.addCleanup(p.stop)

    def stored(self):
        return json.loads(self.jobs.read_text())

    def add(self, **body):
        return server.add_job({"company": "Quiet Co", "title": "Product Lead", "location": "London", **body})

    def test_it_is_saved_as_yours_shortlisted_with_its_history_and_folder(self):
        out = self.add(description=" What the recruiter sent ", folder="/Users/me/Applications/Quiet Co", note="From a recruiter")
        self.assertTrue(out["ok"])
        self.assertRegex(out["id"], r"^manual:quiet-co:[0-9a-f]{8}$")
        j = self.stored()[out["id"]]
        self.assertEqual((j["source"], j["stage"], j["url"], j["score"], j["description"]), ("manual", "shortlisted", "", None, "What the recruiter sent"))
        self.assertEqual((j["folder"], j["note"], j["location"]), ("/Users/me/Applications/Quiet Co", "From a recruiter", "London"))
        self.assertEqual([h["stage"] for h in j["history"]], ["shortlisted"])
        self.assertTrue(j["first_seen"] and j["first_seen"] == j["history"][0]["at"])
        self.assertNotIn("description", out["job"])

    def test_a_company_new_to_gus_becomes_an_account_and_a_known_one_keeps_its_record_and_spelling(self):
        self.assertTrue(self.add()["new_account"])
        c = json.loads(self.companies.read_text())
        self.assertEqual((c["Quiet Co"]["status"], c["Quiet Co"]["source"]), ("watched", "manual"))
        self.assertNotIn("board", c["Quiet Co"])  # none found: still an account, and the full run keeps looking
        self.lookup.reset_mock()
        out = self.add(company="acme ltd", title="Head of Product")
        self.assertFalse(out["new_account"])
        self.assertEqual(out["job"]["company"], "Acme")
        self.assertEqual(json.loads(self.companies.read_text())["Acme"], {"status": "watched", "tier": "A"})
        self.lookup.assert_not_called()  # a company Gus has is left as it is

    def test_a_new_company_has_its_job_board_found_and_watched_in_the_same_step(self):
        self.lookup.return_value = ("lever", "quietco", 7)
        out = self.add(careers="https://quiet.example/careers")
        self.lookup.assert_called_once_with("Quiet Co", "https://quiet.example/careers")
        self.assertEqual((out["new_account"], out["board"], out["open_roles"]), (True, "lever:quietco", 7))
        c = json.loads(self.companies.read_text())["Quiet Co"]
        self.assertEqual((c["board"], c["careers_url"], c["status"]), ("lever:quietco", "https://quiet.example/careers", "watched"))
        self.assertIn("Quiet Co,lever,quietco", self.watchlist.read_text())
        self.assertIn("error", self.add(company="Other Co", careers="quiet.example/careers"))

    def test_the_starting_stage_can_be_chosen_but_not_closed_or_skipped(self):
        self.assertEqual(self.add(stage="applied")["job"]["stage"], "applied")
        for stage in ("closed", "skipped", "nonsense"):
            self.assertIn("error", self.add(title="Other", stage=stage))

    def test_what_cannot_be_saved_is_refused_and_nothing_is_written(self):
        for body in ({"company": " "}, {"title": ""}, {"url": "javascript:alert(1)"}, {"url": "acme.example/jobs"}, {"folder": "ftp://x/y"}):
            self.assertIn("error", self.add(**body), body)
        self.assertFalse(self.jobs.exists())
        self.assertNotIn("Quiet Co", json.loads(self.companies.read_text()))
        self.lookup.assert_not_called()

    def test_a_role_gus_has_already_is_not_added_twice(self):
        first = self.add()
        again = self.add(company="Quiet Co Ltd", title="Product Lead (London)")
        self.assertEqual((again.get("id"), "error" in again), (first["id"], True))
        self.jobs.write_text(json.dumps({"gh:acme:9": {"id": "gh:acme:9", "company": "Acme", "title": "Senior Product Manager", "url": "https://boards.example/acme/9", "stage": "new"}}))
        self.assertEqual(self.add(company="Acme", title="Senior Product Manager")["id"], "gh:acme:9")  # the board's ad, found before
        self.assertEqual(self.add(company="Acme", title="Anything", url="https://boards.example/acme/9/?utm_source=x")["id"], "gh:acme:9")
        self.assertEqual(list(self.stored()), ["gh:acme:9"])

    def test_its_details_can_be_corrected_and_a_new_description_is_scored_again(self):
        jid = self.add(description="First draft")["id"]
        store = self.stored()
        store[jid].update(score=8, why="Good fit", flags=["x"])
        self.jobs.write_text(json.dumps(store))
        out = server.update_job({"id": jid, "title": " Product  Lead, Platform ", "location": "London (hybrid)", "url": "https://quiet.example/jobs/1"})
        self.assertEqual((out["job"]["title"], out["job"]["location"], out["job"]["url"], out["job"]["score"]),
                         ("Product Lead, Platform", "London (hybrid)", "https://quiet.example/jobs/1", 8))
        self.assertEqual(server.update_job({"id": jid, "description": "First draft"})["job"]["score"], 8)  # the same words: nothing to rescore
        server.update_job({"id": jid, "description": "What the recruiter sent next"})
        j = self.stored()[jid]
        self.assertEqual((j["description"], j["score"], j["why"], j["flags"], j["stage"]), ("What the recruiter sent next", None, None, [], "shortlisted"))
        for bad in ({"title": " "}, {"url": "javascript:alert(1)"}):
            self.assertIn("error", server.update_job({"id": jid, **bad}))
        self.assertEqual(self.stored()[jid]["title"], "Product Lead, Platform")

    def test_a_skip_can_say_why_and_the_reason_goes_when_the_role_comes_back(self):
        jid = self.add()["id"]
        self.assertIn("error", server.update_job({"id": jid, "skip_reason": "domain"}))  # not skipped: nothing to explain
        self.assertNotIn("skip_reason", self.stored()[jid])
        self.assertIn("error", server.update_job({"id": jid, "stage": "skipped", "skip_reason": "boring"}))
        self.assertEqual(self.stored()[jid]["stage"], "shortlisted")  # a bad reason changes nothing
        self.assertEqual(server.update_job({"id": jid, "stage": "skipped", "skip_reason": "domain"})["job"]["skip_reason"], "domain")
        self.assertEqual(server.update_job({"id": jid, "skip_reason": "pay"})["job"]["skip_reason"], "pay")
        server.update_job({"id": jid, "skip_reason": ""})
        self.assertNotIn("skip_reason", self.stored()[jid])
        server.update_job({"id": jid, "skip_reason": "junior"})
        server.update_job({"id": jid, "stage": "new"})
        self.assertNotIn("skip_reason", self.stored()[jid])

    def test_a_role_from_a_board_keeps_what_its_ad_says(self):
        self.jobs.write_text(json.dumps({"gh:acme:9": {"id": "gh:acme:9", "company": "Acme", "title": "PM", "url": "https://boards.example/acme/9",
                                                       "stage": "new", "source": "board", "description": "The ad"}}))
        self.assertIn("error", server.update_job({"id": "gh:acme:9", "title": "Something else"}))
        self.assertTrue(server.update_job({"id": "gh:acme:9", "note": "Mine"})["ok"])  # your own note is still yours
        self.assertEqual((self.stored()["gh:acme:9"]["title"], self.stored()["gh:acme:9"]["note"]), ("PM", "Mine"))

    def test_one_role_can_be_read_in_full_with_its_description(self):
        jid = self.add(description="What the recruiter sent")["id"]
        self.assertEqual(server.get_job(jid)["job"]["description"], "What the recruiter sent")
        self.assertIn("error", server.get_job("manual:nobody:00000000"))
        self.assertIn("error", server.get_job(""))

    def test_no_scan_closes_it_even_once_a_board_lists_the_same_role(self):
        jid = self.add()["id"]
        store = self.stored()
        J.absorb(store[jid], {"id": "gh:quietco:7", "url": "https://boards.example/quietco/7", "source": "board"}, NOW)
        for _ in range(3):
            self.assertEqual(mark_closed(store, {"gh:quietco", "manual:quiet-co"}, set(), NOW), [])
        self.assertNotIn("ad_closed", store[jid])
        self.assertNotIn("missed", store[jid])

    def test_a_low_score_is_recorded_but_never_skips_it(self):
        jid = self.add(stage="new")["id"]
        patches = [(triage, "JOBS", self.jobs), (triage, "notify", mock.Mock()), (triage, "load_config", lambda: {"notify_min_score": 8}),
                   (E, "JOBS", self.jobs), (E, "EVENTS", self.dir / "events.json"), (E, "COMPANIES", self.companies),
                   (E, "MOVES", self.dir / "moves.json"), (E, "SIGNALS", self.dir / "signals.json")]
        for target, attr, value in patches:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        profile = self.dir / "profile.md"
        profile.write_text("Made-up candidate")
        with mock.patch.object(triage.ai, "run", return_value=json.dumps([{"id": jid, "score": 2, "why": "Too junior"}])):
            triage.score_batch(profile, list(self.stored().values()))
        j = self.stored()[jid]
        self.assertEqual((j["score"], j["stage"]), (2, "new"))

    def test_a_low_score_at_a_company_you_love_is_shown_to_you_not_skipped(self):
        self.companies.write_text(json.dumps({"Acme": {"status": "watched", "tier": "A", "favourite": True},
                                              "Gone": {"status": "watched", "favourite": True, "vetoed": True},
                                              "Other": {"status": "watched", "tier": "B"}}))
        role = lambda i, company: {"id": i, "company": company, "title": "Product Manager", "location": "London", "url": "", "stage": "new", "score": None}
        self.jobs.write_text(json.dumps({"a": role("a", "Acme Ltd"), "g": role("g", "Gone"), "o": role("o", "Other")}))
        patches = [(triage, "JOBS", self.jobs), (triage, "COMPANIES", self.companies), (triage, "notify", mock.Mock()),
                   (triage, "load_config", lambda: {"notify_min_score": 8}),
                   (E, "JOBS", self.jobs), (E, "EVENTS", self.dir / "events.json"), (E, "COMPANIES", self.companies),
                   (E, "MOVES", self.dir / "moves.json"), (E, "SIGNALS", self.dir / "signals.json")]
        for target, attr, value in patches:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        profile = self.dir / "profile.md"
        profile.write_text("Made-up candidate")
        reply = json.dumps([{"id": i, "score": 3, "why": "Wrong area"} for i in "ago"])
        with mock.patch.object(triage.ai, "run", return_value=reply):
            triage.score_batch(profile, list(self.stored().values()))
        self.assertEqual({i: (j["score"], j["stage"]) for i, j in self.stored().items()},
                         {"a": (3, "new"), "g": (3, "skipped"), "o": (3, "skipped")})

    def test_with_no_ad_to_link_to_it_passes_the_data_check_and_is_never_a_role_event(self):
        jid = self.add()["id"]
        store = self.stored()
        store[jid]["score"] = 9
        self.jobs.write_text(json.dumps(store))
        self.assertEqual(schema.check(self.dir), {})
        board = dict(store[jid], source="board")
        self.assertEqual(schema.check_record("jobs", jid, board, {}, {}), ["missing url"])
        patches = [(E, "JOBS", self.jobs), (E, "EVENTS", self.dir / "events.json"), (E, "COMPANIES", self.companies),
                   (E, "MOVES", self.dir / "moves.json"), (E, "SIGNALS", self.dir / "signals.json")]
        for target, attr, value in patches:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        E.sync()
        self.assertNotIn(f"role:{jid}", json.loads((self.dir / "events.json").read_text() or "{}") if (self.dir / "events.json").exists() else {})


if __name__ == "__main__":
    unittest.main()
