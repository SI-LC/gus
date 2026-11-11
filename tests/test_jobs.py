"""One role, however many places list it: the same ad from two sources is one record, and duplicates already saved merge."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import jobs as J

NOW = "2026-11-08T10:00:00+00:00"


def role(jid, company="Acme", title="Product Manager", url=None, **more):
    return {"id": jid, "company": company, "title": title, "url": url or f"https://jobs.example/{jid}", **more}


class Matching(unittest.TestCase):
    def test_tracking_tags_and_www_dont_make_a_new_address(self):
        a = J.same_url("https://www.Acme.com/jobs/123/?utm_source=linkedin&gh_src=abc#top")
        self.assertEqual(a, "https://acme.com/jobs/123")
        self.assertEqual(J.same_url("http://acme.com/jobs?gh_jid=5"), "https://acme.com/jobs?gh_jid=5")  # the job-picking key stays

    def test_an_absorbed_id_is_known(self):
        store = {"adzuna:1": role("adzuna:1", also={"gh:acme:9": {"url": "https://boards.greenhouse.io/acme/jobs/9"}})}
        self.assertEqual(J.find_match(store, role("gh:acme:9")), "adzuna:1")

    def test_the_same_address_is_the_same_role(self):
        store = {"gh:acme:9": role("gh:acme:9", title="Senior PM", url="https://boards.greenhouse.io/acme/jobs/9")}
        found = role("jsearch:x", title="Senior Product Manager, Payments", url="https://boards.greenhouse.io/acme/jobs/9/?utm_source=jsearch")
        self.assertEqual(J.find_match(store, found), "gh:acme:9")

    def test_same_company_and_title_from_another_source_is_the_same_role(self):
        store = {"gh:acme:9": role("gh:acme:9", title="Senior Product Manager", stage="new")}
        self.assertEqual(J.find_match(store, role("adzuna:7", company="Acme Ltd", title="Senior Product Manager - London")), "gh:acme:9")

    def test_two_ids_on_one_board_are_two_roles(self):
        store = {"gh:acme:9": role("gh:acme:9", stage="new")}
        self.assertIsNone(J.find_match(store, role("gh:acme:10")))

    def test_a_closed_role_doesnt_claim_a_fresh_one(self):
        store = {"adzuna:1": role("adzuna:1", ad_closed="x"), "gh:acme:2": role("gh:acme:2", stage="closed")}
        self.assertIsNone(J.find_match(store, role("jsearch:3")))

    def test_a_role_you_skipped_doesnt_come_back_as_new_from_another_site(self):
        store = {"gh:acme:2": role("gh:acme:2", stage="skipped")}
        self.assertEqual(J.find_match(store, role("getro:jobs.vc.example:3")), "gh:acme:2")

    def test_capitals_in_an_address_dont_make_a_second_role(self):
        store = {"ashby:acme:1": role("ashby:acme:1", title="Other", url="https://jobs.ashbyhq.com/acme/7afe97a2")}
        self.assertEqual(J.find_match(store, role("getro:x:9", title="Else", url="https://jobs.ashbyhq.com/Acme/7afe97a2")), "ashby:acme:1")

    def test_absorb_remembers_the_sighting_and_fills_gaps(self):
        have = role("adzuna:1", location="", description="short précis", text_kind="summary", stage="shortlisted")
        J.absorb(have, role("gh:acme:9", location="London", description="The employer's full ad", source="board"), NOW)
        self.assertEqual(have["also"]["gh:acme:9"]["source"], "board")
        self.assertEqual((have["location"], have["description"], have["text_kind"], have["stage"], have["last_seen"]),
                         ("London", "The employer's full ad", "posting", "shortlisted", NOW))


class Dedupe(unittest.TestCase):
    def test_groups_by_address_and_by_title_across_boards(self):
        store = {"gh:acme:1": role("gh:acme:1", url="https://a.com/1"), "adzuna:2": role("adzuna:2", url="https://a.com/1/?utm_source=x"),
                 "jsearch:3": role("jsearch:3", title="Product Manager (London)", stage="new"),
                 "gh:acme:4": role("gh:acme:4", title="Product Manager", stage="new"),  # same board as gh:acme:1: a second PM role
                 "gh:other:5": role("gh:other:5", company="Other")}
        groups = sorted(sorted(g) for g in J.groups(store))
        self.assertEqual(groups, [["adzuna:2", "gh:acme:1"]])  # jsearch:3 could be either PM role on Acme's board: left alone
        del store["gh:acme:4"]
        self.assertEqual(sorted(sorted(g) for g in J.groups(store)), [["adzuna:2", "gh:acme:1", "jsearch:3"]])

    def test_merge_keeps_the_furthest_along_and_the_earliest_date(self):
        store = {"adzuna:1": role("adzuna:1", stage="new", first_seen="2026-11-01", score=8, why="good", history=[]),
                 "gh:acme:2": role("gh:acme:2", stage="applied", first_seen="2026-11-05", score=None,
                                   history=[{"stage": "applied", "at": "2026-11-06"}], note="spoke to Sam")}
        keep, gone = J.merge(store, ["adzuna:1", "gh:acme:2"], NOW)
        self.assertEqual((keep, gone), ("gh:acme:2", ["adzuna:1"]))
        j = store["gh:acme:2"]
        self.assertEqual((j["first_seen"], j["score"], j["why"], j["note"], j["stage"]), ("2026-11-01", 8, "good", "spoke to Sam", "applied"))
        self.assertIn("adzuna:1", j["also"])
        self.assertNotIn("adzuna:1", store)

    def test_events_and_moves_follow_the_kept_role(self):
        events = {"role:adzuna:1": {"type": "role_posted", "job_id": "adzuna:1", "source": "job:adzuna:1"}}
        moves = {"2026-W45:acme:1": {"event_id": "role:adzuna:1"}}
        J.repoint(events, moves, "gh:acme:2", ["adzuna:1"])
        self.assertEqual(list(events), ["role:gh:acme:2"])
        self.assertEqual(events["role:gh:acme:2"]["job_id"], "gh:acme:2")
        self.assertEqual(moves["2026-W45:acme:1"]["event_id"], "role:gh:acme:2")
        # both had an event: the kept one's stays, the other goes
        events = {"role:a": {"x": 1}, "role:b": {"x": 2}}
        J.repoint(events, {}, "b", ["a"])
        self.assertEqual(events, {"role:b": {"x": 2}})

    def test_dry_run_changes_nothing_and_apply_merges(self):
        import json
        from radar import accounts, events, scan
        with tempfile.TemporaryDirectory() as d:
            jobs, ev, mv = Path(d) / "jobs.json", Path(d) / "events.json", Path(d) / "moves.json"
            jobs.write_text(json.dumps({"adzuna:1": role("adzuna:1", stage="new"), "gh:acme:2": role("gh:acme:2", stage="shortlisted")}))
            ev.write_text(json.dumps({"role:adzuna:1": {"job_id": "adzuna:1"}}))
            mv.write_text("{}")
            with mock.patch.object(scan, "JOBS", jobs), mock.patch.object(events, "EVENTS", ev), mock.patch.object(accounts, "MOVES", mv):
                self.assertEqual(J.dedupe(), [("gh:acme:2", ["adzuna:1", "gh:acme:2"])])
                self.assertEqual(set(json.loads(jobs.read_text())), {"adzuna:1", "gh:acme:2"})
                self.assertEqual(J.dedupe(apply=True), [("gh:acme:2", ["adzuna:1"])])
                self.assertEqual(set(json.loads(jobs.read_text())), {"gh:acme:2"})
                self.assertEqual(list(json.loads(ev.read_text())), ["role:gh:acme:2"])


class InTheScan(unittest.TestCase):
    def test_a_board_role_already_found_on_a_job_site_is_absorbed_not_added(self):
        import json
        from radar import scan
        with tempfile.TemporaryDirectory() as d:
            jobs = Path(d) / "jobs.json"
            jobs.write_text(json.dumps({"adzuna:1": role("adzuna:1", stage="new", source="adzuna", location="London, UK")}))
            found = [role("gh:acme:9", location="London", description="Full ad")]
            with mock.patch.object(scan, "JOBS", jobs), mock.patch.object(scan, "DATA", Path(d)), \
                 mock.patch.object(scan, "fetch_all", lambda: (found, [])), mock.patch.object(scan, "matches", lambda *a: True), \
                 mock.patch("sys.argv", ["scan", "--json"]), mock.patch("builtins.print"):
                scan.main()
            store = json.loads(jobs.read_text())
            self.assertEqual(list(store), ["adzuna:1"])
            self.assertIn("gh:acme:9", store["adzuna:1"]["also"])

    def test_an_absorbed_role_stays_live_while_its_board_lists_it(self):
        from radar.scan import mark_closed
        store = {"adzuna:1": {"also": {"gh:acme:9": {}}}}
        mark_closed(store, {"gh:acme"}, {"gh:acme:9"}, NOW); mark_closed(store, {"gh:acme"}, {"gh:acme:9"}, NOW)
        self.assertNotIn("ad_closed", store["adzuna:1"])
        mark_closed(store, {"gh:acme"}, set(), NOW); mark_closed(store, {"gh:acme"}, set(), NOW)
        self.assertEqual(store["adzuna:1"]["ad_closed"], NOW)


if __name__ == "__main__":
    unittest.main()
