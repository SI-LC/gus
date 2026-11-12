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

    def test_an_ad_put_up_again_on_its_board_is_the_role_you_applied_for(self):
        store = {"sr:acme:1": role("sr:acme:1", stage="applied", ref="R100")}
        self.assertEqual(J.find_match(store, role("sr:acme:2", ref="R100")), "sr:acme:1")
        self.assertIsNone(J.find_match(store, role("sr:acme:3", ref="R200")))  # another vacancy with the same title
        self.assertIsNone(J.find_match(store, role("sr:acme:4", title="Product Manager, Payments", ref="R100")))  # one number can cover several ads

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

    def test_groups_an_ad_with_its_re_post_on_the_same_board(self):
        store = {"sr:acme:1": role("sr:acme:1", stage="applied", ref="R100"), "sr:acme:2": role("sr:acme:2", stage="new", ref="R100"),
                 "sr:acme:3": role("sr:acme:3", stage="new", ref="R200"), "sr:acme:4": role("sr:acme:4", stage="new")}
        self.assertEqual(sorted(sorted(g) for g in J.groups(store)), [["sr:acme:1", "sr:acme:2"]])

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
        from radar import accounts, events
        with tempfile.TemporaryDirectory() as d:
            jobs, ev, mv = Path(d) / "jobs.json", Path(d) / "events.json", Path(d) / "moves.json"
            jobs.write_text(json.dumps({"adzuna:1": role("adzuna:1", stage="new"), "gh:acme:2": role("gh:acme:2", stage="shortlisted")}))
            ev.write_text(json.dumps({"role:adzuna:1": {"job_id": "adzuna:1"}}))
            mv.write_text("{}")
            with mock.patch.object(J, "JOBS", jobs), mock.patch.object(events, "EVENTS", ev), mock.patch.object(accounts, "MOVES", mv):
                self.assertEqual(J.dedupe(), [("gh:acme:2", ["adzuna:1", "gh:acme:2"])])
                self.assertEqual(set(json.loads(jobs.read_text())), {"adzuna:1", "gh:acme:2"})
                self.assertEqual(J.dedupe(apply=True), [("gh:acme:2", ["adzuna:1"])])
                self.assertEqual(set(json.loads(jobs.read_text())), {"gh:acme:2"})
                self.assertEqual(list(json.loads(ev.read_text())), ["role:gh:acme:2"])


class InTheScan(unittest.TestCase):
    def test_a_board_role_already_found_on_a_job_site_is_absorbed_not_added(self):
        import json
        from radar import matching, scan
        with tempfile.TemporaryDirectory() as d:
            jobs = Path(d) / "jobs.json"
            jobs.write_text(json.dumps({"adzuna:1": role("adzuna:1", stage="new", source="adzuna", location="London, UK")}))
            found = [role("gh:acme:9", location="London", description="Full ad")]
            with mock.patch.object(scan, "JOBS", jobs), mock.patch.object(scan, "DATA", Path(d)), \
                 mock.patch.object(scan, "fetch_all", lambda: (found, [])), mock.patch.object(matching, "matches", lambda *a: True), \
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


class AddedByAnAgent(unittest.TestCase):
    """A role an agent finds on a careers page goes through the same checks as every other source, in code."""

    def setUp(self):
        from sample_search import use_sample
        use_sample(self)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.jobs = Path(d.name) / "jobs.json"
        patcher = mock.patch.object(J, "JOBS", self.jobs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, have=None, **found):
        import json
        self.jobs.write_text(json.dumps(have or {}))
        row = {"company": "Revolut", "title": "Senior Product Manager", "location": "London", "url": "https://revolut.example/careers/1", **found}
        return J.add([row]), json.loads(self.jobs.read_text())

    def test_a_new_role_is_added_unscored_and_adding_it_again_changes_nothing(self):
        import json
        out, store = self.add()
        (what, jid), = out
        self.assertEqual((what, store[jid]["stage"], store[jid]["score"], store[jid]["source"]), ("added", "new", None, "web"))
        again = J.add([{"company": "Revolut", "title": "Senior Product Manager", "location": "London", "url": "https://revolut.example/careers/1/?utm_source=x"}])
        self.assertEqual((again, list(json.loads(self.jobs.read_text()))), ([("have", jid)], [jid]))

    def test_a_role_gus_has_from_a_job_site_is_a_sighting_not_a_second_role(self):
        have = {"jsearch:9": role("jsearch:9", company="Revolut Ltd", title="Senior Product Manager", stage="shortlisted", source="jsearch")}
        out, store = self.add(have)
        self.assertEqual((out, list(store), store["jsearch:9"]["stage"]), ([("have", "jsearch:9")], ["jsearch:9"], "shortlisted"))
        self.assertEqual([a["source"] for a in store["jsearch:9"]["also"].values()], ["web"])

    def test_a_role_you_skipped_stays_skipped(self):
        have = {"adzuna:4": role("adzuna:4", company="Revolut", title="Senior Product Manager", stage="skipped")}
        out, store = self.add(have)
        self.assertEqual((out[0][0], list(store), store["adzuna:4"]["stage"]), ("have", ["adzuna:4"], "skipped"))

    def test_the_same_address_is_the_same_role_whatever_the_title_says(self):
        have = {"gh:revolut:1": role("gh:revolut:1", company="Revolut", title="Sr PM, Cards", url="https://revolut.example/careers/1")}
        self.assertEqual(self.add(have)[0], [("have", "gh:revolut:1")])

    def test_a_role_outside_your_titles_or_places_is_turned_down_with_the_reason(self):
        for change in ({"title": "Senior Product Designer"}, {"location": "New York"}):
            out, store = self.add(**change)
            self.assertEqual((out[0][0], store), ("no", {}))
            self.assertIn("titles and locations", out[0][1])

    def test_a_role_without_its_own_address_is_turned_down(self):
        for change in ({"url": "revolut.example/careers"}, {"url": ""}, {"company": ""}):
            self.assertEqual(self.add(**change)[0][0][0], "no")


class AdvertisedAgain(unittest.TestCase):
    """A role you're pursuing, put up again, is your application and not a new role in the inbox."""

    def setUp(self):
        self.store = {"gh:acme:1": {"id": "gh:acme:1", "company": "Acme", "title": "Senior Product Manager", "location": "London",
                                    "stage": "applied", "first_seen": "2026-10-01T09:00:00"}}
        self.again = {"id": "gh:acme:2", "company": "Acme", "title": "Senior Product Manager", "location": "London",
                      "url": "https://boards.example/acme/2", "source": "board"}

    def test_a_new_ad_for_the_role_you_applied_for_is_that_role(self):
        self.assertEqual(J.find_match(self.store, self.again), "gh:acme:1")

    def test_it_is_still_that_role_after_the_first_ad_closed(self):
        self.store["gh:acme:1"]["ad_closed"] = "2026-10-20T09:00:00"
        self.assertEqual(J.find_match(self.store, self.again), "gh:acme:1")

    def test_every_stage_from_shortlisted_to_interviewing_counts(self):
        for stage in ("shortlisted", "preparing", "applied", "interviewing"):
            self.store["gh:acme:1"]["stage"] = stage
            self.assertEqual(J.find_match(self.store, self.again), "gh:acme:1", stage)

    def test_a_role_you_have_not_chosen_stays_two_roles_on_one_board(self):
        for stage in ("new", "skipped", "closed"):
            self.store["gh:acme:1"]["stage"] = stage
            self.assertIsNone(J.find_match(self.store, self.again), stage)

    def test_the_same_title_in_another_city_is_another_opening(self):
        self.assertIsNone(J.find_match(self.store, {**self.again, "location": "Berlin"}))

    def test_the_employers_own_vacancy_numbers_decide_where_it_gives_them(self):
        self.store["gh:acme:1"]["ref"] = "R100"
        self.assertIsNone(J.find_match(self.store, {**self.again, "ref": "R200"}))
        self.assertEqual(J.find_match(self.store, {**self.again, "ref": "R100"}), "gh:acme:1")

    def test_a_different_title_at_the_company_is_a_new_role(self):
        self.assertIsNone(J.find_match(self.store, {**self.again, "title": "Product Lead"}))

    def test_when_it_was_advertised_again_is_kept_once(self):
        mine = self.store["gh:acme:1"]
        J.absorb(mine, self.again, "2026-11-12T08:00:00")
        self.assertEqual(mine["readvertised"], "2026-11-12T08:00:00")
        self.assertEqual(mine["also"]["gh:acme:2"]["url"], "https://boards.example/acme/2")
        J.absorb(mine, self.again, "2026-11-13T08:00:00")  # the next scan sees the same ad
        self.assertEqual(mine["readvertised"], "2026-11-12T08:00:00")

    def test_the_same_ad_listed_on_a_job_site_is_not_advertised_again(self):
        mine = self.store["gh:acme:1"]
        J.absorb(mine, {**self.again, "id": "adzuna:99", "source": "adzuna"}, "2026-11-12T08:00:00")
        self.assertNotIn("readvertised", mine)

    def test_a_role_still_in_the_inbox_is_never_marked(self):
        mine = {**self.store["gh:acme:1"], "stage": "new", "ref": "R1"}
        J.absorb(mine, {**self.again, "ref": "R1"}, "2026-11-12T08:00:00")
        self.assertNotIn("readvertised", mine)

