"""Job sources must clean every role the same way and can never go over a limit. No network: fetching is faked."""
import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from radar import settings, sources

CFG = {**{k: re.compile(v, re.I) if k.endswith(("include", "exclude")) else v for k, v in settings.load().items()},
       "source_adzuna": True, "source_jsearch": True, "sources_searches": "product manager\nproduct lead", "adzuna_pages_per_search": 3}
TODAY = date.today()


def role(n, company="Acme", title=None, posted=None, location="London, UK", text="Own the roadmap."):
    return {"id": f"adzuna:{n}", "company": company, "title": title or f"Product Manager {n}", "url": "https://example.com",
            "posted": posted or str(TODAY), "location": location, "description": text}


class Clean(unittest.TestCase):
    def test_the_same_advert_under_another_title_is_a_repeat(self):
        text = "Own the roadmap for our platform. " * 10
        kept, dropped = self.reasons([role(1, text=text), role(2, text=text)])
        self.assertEqual((len(kept), dropped["repeat"]), (1, 1))

    def test_trialled_agencies_go_and_real_employers_stay(self):
        cfg = CFG["source_company_exclude"]
        self.assertTrue(cfg.search("Sterling Bridge Limited"))
        self.assertTrue(cfg.search("SR2 REC LTD"))
        self.assertFalse(cfg.search("Talent Systems"))
        self.assertFalse(cfg.search("United Talent Agency"))

    def reasons(self, rows, store=None):
        return sources.clean(rows, store or {}, CFG)

    def test_keeps_a_real_role(self):
        self.assertEqual(len(self.reasons([role(1)])[0]), 1)

    def test_removes_old_ads_and_ads_with_no_date(self):
        kept, dropped = self.reasons([role(1, posted=str(TODAY - timedelta(days=30))), {**role(2), "posted": None}])
        self.assertEqual((kept, dropped["old"]), ([], 2))

    def test_removes_agencies_by_name_and_wording(self):
        kept, dropped = self.reasons([role(1, company="Deerfoot Recruitment"), role(2, text="We act on behalf of our client.")])
        self.assertEqual((kept, dropped["agency"]), ([], 2))

    def test_removes_blank_employers(self):
        self.assertEqual(self.reasons([role(1, company="")])[1]["no employer"], 1)

    def test_a_role_gus_already_has_from_a_board_is_a_repeat(self):
        store = {"gh:acme:1": {"company": "Acme Ltd", "title": "Product Manager 1"}}
        self.assertEqual(self.reasons([role(1)], store)[1]["repeat"], 1)

    def test_same_role_twice_in_one_pull_is_kept_once(self):
        self.assertEqual(len(self.reasons([role(1), {**role(1), "id": "adzuna:2"}])[0]), 1)

    def test_uses_the_normal_title_and_location_rules(self):
        kept, dropped = self.reasons([role(1, title="Sales Manager"), role(2, location="Berlin, Germany")])
        self.assertEqual((kept, dropped["not a match"]), ([], 2))

    def test_an_empty_setting_removes_nothing(self):
        cfg = {**CFG, "source_company_exclude": re.compile(""), "source_text_exclude": re.compile("")}
        self.assertEqual(len(sources.clean([role(1)], {}, cfg)[0]), 1)


class Limits(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for patch in (mock.patch.object(sources, "STATE", Path(d.name) / "state.json"), mock.patch.object(sources, "DATA", Path(d.name))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_adzuna_stops_at_the_lowest_limit(self):
        st = {"adzuna": {"days": {str(TODAY): 240}}}
        self.assertEqual(sources.budget("adzuna", {**CFG, "adzuna_limit_day": 250}, st), 10)
        self.assertEqual(sources.budget("adzuna", {**CFG, "adzuna_limit_day": 240}, st), 0)

    def test_requests_are_counted_even_if_they_fail(self):
        st = {}
        with mock.patch.dict(sources.FETCH, adzuna=mock.Mock(side_effect=sources.Failed(502))):
            run = sources.collect("adzuna", CFG, st, 30, 14)
        self.assertEqual((sum(st["adzuna"]["days"].values()), run["faults"]), (2, 2))

    def test_a_refusal_stops_the_source(self):
        st, fetch = {}, mock.Mock(side_effect=sources.Failed(429))
        with mock.patch.dict(sources.FETCH, adzuna=fetch):
            run = sources.collect("adzuna", CFG, st, 30, 14)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("refused", run["stopped"])

    def test_a_run_never_spends_more_than_its_allowance(self):
        st, fetch = {}, mock.Mock(side_effect=lambda t, p, c, a: [role(f"{t}{p}{i}") for i in range(50)])
        with mock.patch.dict(sources.FETCH, adzuna=fetch):
            sources.collect("adzuna", {**CFG, "adzuna_pages_per_search": 10}, st, 3, 14)
        self.assertEqual(fetch.call_count, 3)

    def test_jsearch_makes_no_requests_without_a_figure_from_today(self):
        old = {"limit": 200, "used": 0, "remaining": 200, "reset_at": "2026-12-05T00:00:00Z", "checked": "2020-01-01"}
        self.assertEqual(sources.budget("jsearch", CFG, {"jsearch": {"quota": old}}), 0)
        self.assertEqual(sources.budget("jsearch", CFG, {}), 0)

    def quota(self, used, limit=200):
        return {"limit": limit, "used": used, "remaining": limit - used, "reset_at": "2026-12-05T00:00:00Z", "checked": str(TODAY)}

    def test_jsearch_stops_at_the_allowance_you_set(self):
        st = {"jsearch": {"quota": self.quota(170)}}
        cfg = {**CFG, "jsearch_allow_month": 180, "jsearch_every_days": 7}
        self.assertEqual(sources.budget("jsearch", cfg, st), 10)
        row = sources.limits("jsearch", cfg, st)[0]
        self.assertEqual((row["limit"], row["left"], row["resets"]), (180, 10, "2026-12-05"))
        self.assertEqual(sources.limits("jsearch", {**cfg, "jsearch_allow_month": 170}, st)[0]["left"], 0)

    def test_the_allowance_is_shared_across_runs(self):
        self.assertEqual(sources.per_run("jsearch", {**CFG, "jsearch_allow_month": 180, "jsearch_every_days": 1}), 6)
        self.assertEqual(sources.per_run("jsearch", {**CFG, "jsearch_allow_month": 180, "jsearch_every_days": 7}), 42)

    def test_a_cut_short_run_keeps_the_older_window(self):
        st = {"adzuna": {"covered": str(TODAY - timedelta(days=9)), "last_run": str(TODAY - timedelta(days=2))}}
        ages = []
        with mock.patch.object(sources, "JOBS", Path(sources.STATE).parent / "jobs.json"), \
             mock.patch.object(sources, "collect", side_effect=lambda n, c, s, l, a: ages.append(a) or {"rows": {}, "spent": 1, "empty": 0, "faults": 1, "cut": 0, "stopped": ""}):
            sources.run_source("adzuna", {**CFG, "sources_max_age_days": 14}, st)
        self.assertEqual(ages, [11])
        self.assertEqual(st["adzuna"]["covered"], str(TODAY - timedelta(days=9)))

    def test_a_weekly_jsearch_run_only_asks_for_a_week(self):
        with mock.patch.object(sources, "call", return_value={}) as call:
            sources.jsearch("pm", 1, CFG, 7 + sources.OVERLAP)
        self.assertIn("date_posted=week", call.call_args.args[0])

    def test_a_bad_usage_reply_is_not_trusted(self):
        reply = {"data": {"plan": {}, "quotas": [{"limit": 200, "used": 0, "remaining": 200, "reset_at": None}]}}
        with mock.patch.object(sources, "call", return_value=reply):
            self.assertFalse(sources.refresh_quota({}))

    def test_one_source_failing_doesnt_stop_the_other(self):
        ran = []
        def run_one(name, cfg, st):
            ran.append(name)
            raise RuntimeError("boom")
        with mock.patch.object(sources, "run_one", run_one), mock.patch.object(sources, "load_config", return_value=CFG), \
             mock.patch.object(sources, "secret", return_value="x"):
            sources.main()
        self.assertEqual(ran, ["adzuna", "jsearch"])

    def test_a_limit_reached_during_a_run_is_reported(self):
        st = {"adzuna": {"days": {str(TODAY): 249}}}
        cfg = {**CFG, "adzuna_limit_day": 250}
        def run(name, c, s):
            sources.spend(s, name)
            return "done"
        with mock.patch.object(sources, "run_source", run), mock.patch.object(sources, "secret", return_value="x"), mock.patch("radar.notify.notify") as told:
            sources.run_one("adzuna", cfg, st)
        self.assertEqual(told.call_count, 1)

    def test_a_skipped_run_says_when_its_next_due(self):
        st = {"adzuna": {"last_run": str(TODAY)}}
        self.assertIn("next due", sources.run_one("adzuna", {**CFG, "adzuna_every_days": 2}, st))

    def test_old_untouched_roles_are_closed(self):
        old = str(TODAY - timedelta(days=60))
        store = {"a": {"source": "adzuna", "stage": "new", "posted": old}, "b": {"source": "adzuna", "stage": "shortlisted", "posted": old},
                 "c": {"source": "board", "stage": "new", "posted": old}, "d": {"source": "jsearch", "stage": "new", "posted": str(TODAY)}}
        sources.close_old(store, {**CFG, "sources_close_after_days": 45}, "now")
        self.assertEqual([k for k, j in store.items() if j.get("ad_closed")], ["a"])

    def warn(self, st=None, **cfg):
        with mock.patch.object(sources, "secret", return_value="x"):
            return " ".join(sources.warnings(cfg.pop("name", "jsearch"), {**CFG, **cfg}, st or {}))

    def test_warns_about_a_free_plan_running_every_day(self):
        q = {**self.quota(0), "free": True}
        text = self.warn({"jsearch": {"quota": q}}, jsearch_allow_month=180, jsearch_every_days=1, jsearch_pages_per_search=3,
                         sources_searches="a\nb\nc\nd\ne\nf\ng\nh")
        self.assertIn("free plan", text)
        self.assertIn("6 requests", text)

    def test_warns_when_allowance_is_above_the_plan(self):
        self.assertIn("gives 200", self.warn({"jsearch": {"quota": self.quota(0)}}, jsearch_allow_month=500))

    def test_warns_about_a_missing_key(self):
        with mock.patch.object(sources, "secret", return_value=""):
            self.assertIn("key is missing", " ".join(sources.warnings("jsearch", CFG, {})))

    def test_warns_until_adzuna_limits_are_confirmed(self):
        self.assertIn("aren't confirmed", self.warn(name="adzuna"))
        self.assertNotIn("aren't confirmed", self.warn(name="adzuna", adzuna_limits_confirmed=True))

    def test_warns_when_a_search_was_cut_short(self):
        self.assertIn("older ones were missed", self.warn({"adzuna": {"cut": 1}}, name="adzuna"))

    def test_the_default_settings_raise_no_warning(self):
        d = {**settings.load(), "source_adzuna": True, "adzuna_limits_confirmed": True}
        with mock.patch.object(sources, "secret", return_value="x"):
            self.assertEqual(sources.warnings("adzuna", d, {}), [])
            self.assertEqual(sources.warnings("jsearch", d, {}), [])

    def test_runs_only_when_due(self):
        st = {"adzuna": {"last_run": str(TODAY - timedelta(days=1))}}
        self.assertFalse(sources.is_due("adzuna", {**CFG, "adzuna_every_days": 2}, st))
        self.assertTrue(sources.is_due("adzuna", {**CFG, "adzuna_every_days": 1}, st))

    def test_reset_dates(self):
        self.assertEqual(sources.resets("month", date(2026, 11, 30)), date(2026, 12, 1))
        self.assertEqual(sources.resets("week", date(2026, 11, 5)), date(2026, 11, 9))

    def test_the_limit_notification_is_sent_once(self):
        row = {"period": "day", "resets": "2026-11-06"}
        with mock.patch("radar.notify.notify") as told:
            st = {}
            sources.tell_limit("adzuna", st, row)
            sources.tell_limit("adzuna", st, row)
        self.assertEqual(told.call_count, 1)


class Reading(unittest.TestCase):
    def test_a_failed_address_never_appears_in_an_error(self):
        self.assertEqual(str(sources.Failed(429)), "HTTP 429")

    def test_adzuna_and_jsearch_become_the_same_shape(self):
        az = {"results": [{"id": 1, "title": "PM", "company": {"display_name": "Acme"}, "created": "2026-11-05T00:00:00Z", "redirect_url": "u",
                           "location": {"display_name": "London", "area": ["UK", "London"]}, "description": "x"}]}
        js = {"data": {"jobs": [{"job_id": "1", "job_title": "PM", "employer_name": "Acme", "job_posted_at_datetime_utc": "2026-11-05T00:00:00Z",
                                 "job_apply_link": "u", "job_location": "Anywhere", "job_description": "x"}]}}
        with mock.patch.object(sources, "call", return_value=az):
            a = sources.adzuna("pm", 1, CFG, 14)[0]
        with mock.patch.object(sources, "call", return_value=js):
            j = sources.jsearch("pm", 1, CFG, 14)[0]
        self.assertEqual(set(a) - {"salary_max"}, set(j))
        self.assertEqual((a["location"], j["location"]), ("London, UK", "Remote, UK"))

    def test_adzuna_links_dont_carry_the_app_id(self):
        reply = {"results": [{"id": 1, "redirect_url": "https://www.adzuna.co.uk/jobs/details/1?utm_medium=api&utm_source=abc123"}]}
        with mock.patch.object(sources, "call", return_value=reply):
            self.assertNotIn("abc123", sources.adzuna("pm", 1, CFG, 14)[0]["url"])

    def test_the_location_comes_from_settings(self):
        with mock.patch.object(sources, "call", return_value={}) as call:
            sources.adzuna("pm", 1, {**CFG, "sources_location": "Leeds", "sources_country": "gb"}, 14)
            sources.jsearch("pm", 1, {**CFG, "sources_location": "Leeds"}, 14)
        self.assertIn("where=Leeds", call.call_args_list[0].args[0])
        self.assertIn("in+Leeds", call.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
