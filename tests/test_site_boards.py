"""Company careers sites: read only what each site publishes, take no for an answer. No network: replies are saved samples."""
import csv
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from radar import resolve, scan
from sample_search import use_sample

SAMPLES = Path(__file__).parent / "samples"
SF = "careers.optimizely.com"
WD = "salesforce.wd12/External_Career_Site"
WD_HOST = "salesforce.wd12.myworkdayjobs.com"


def sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def refused(url, code=403):
    return urllib.error.HTTPError(url, code, "No", {}, None)


class Site:
    """Stands in for the web and notes every address asked for. A reply can be bytes, an exception, or a function of the address."""

    def __init__(self, replies, **first):
        """`first` are checked before `replies`, so a test can answer one address differently."""
        self.replies, self.asked, self.bodies = {**first, **{k: v for k, v in replies.items() if k not in first}}, [], []

    def __call__(self, url, timeout=30, body=None, headers=None):
        self.asked.append(url)
        self.bodies.append(body)
        for part, reply in self.replies.items():
            if part in url:
                reply = reply(url) if callable(reply) else reply
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise refused(url, 404)

    def pages(self):
        return [u for u in self.asked if "/job/" in u]

    def lists(self):
        return [u for u in self.asked if u.endswith("/jobs")]

    def filters(self):
        """What each request for a list of jobs asked to be filtered by."""
        return [json.loads(b)["appliedFacets"] for b in self.bodies if b]


def successfactors_site(**more):
    return Site({f"{SF}/robots.txt": sample("successfactors_robots.txt"), f"{SF}/sitemap.xml": sample("successfactors_sitemap.xml"),
                 f"{SF}/job/": sample("successfactors_job.html")}, **more)


def workday_site(**more):
    return Site({f"{WD_HOST}/robots.txt": sample("workday_robots.txt"), "External_Career_Site/jobs": sample("workday_list.json"),
                 "External_Career_Site/job/": sample("workday_job.json")}, **more)


class Base(unittest.TestCase):
    def setUp(self):
        use_sample(self)
        self.dir = Path(tempfile.mkdtemp())
        for name, value in (("DATA", self.dir), ("JOBS", self.dir / "jobs.json"), ("LOOKED_UP", self.dir / "seen.json"),
                            ("WATCHLIST", self.dir / "w.csv"), ("PAGE_PAUSE", 0)):
            patcher = mock.patch.object(scan, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clear()
        self.addCleanup(self.clear)

    def clear(self):
        scan._known, scan._looked_up = None, None
        scan._listed.clear()
        scan._robots.clear()

    def fetch(self, site):
        return mock.patch.object(scan, "fetch_bytes", site)

    def watch(self, *rows):
        with (self.dir / "w.csv").open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["company", "ats", "slug", "sector"])
            writer.writerows(rows)

    def run_scan(self, site):
        """Fetch all boards as a scan does, then return (jobs, errors written for the Health page)."""
        self.clear()
        cfg = {**scan.load_config(), "source_hn": False}
        with self.fetch(site), mock.patch.object(scan, "load_config", lambda: cfg):
            jobs, _ = scan.fetch_all()
        return jobs, json.loads((self.dir / "board_errors.json").read_text())


class Robots(unittest.TestCase):
    def allows(self, text, path):
        return scan.robots_allow(scan.parse_robots(text)[0], path)

    def test_optimizely_blocks_its_rss_but_not_its_jobs(self):
        text = sample("successfactors_robots.txt").decode()
        self.assertFalse(self.allows(text, "/services/rss/job/"))
        self.assertTrue(self.allows(text, "/job/London-Product-Manager/1/"))
        self.assertTrue(self.allows(text, "/sitemap.xml"))

    def test_a_workday_site_lists_its_sitemaps_and_blocks_what_it_says(self):
        text = sample("workday_robots.txt").decode()
        self.assertTrue(self.allows(text, "/External_Career_Site/siteMap.xml"))
        self.assertFalse(self.allows(text, "/refreshFacet/x"))
        self.assertIn(f"https://{WD_HOST}/External_Career_Site/siteMap.xml", scan.parse_robots(text)[1])

    def test_the_longest_rule_wins_whatever_the_order(self):
        self.assertFalse(self.allows("User-agent: *\nAllow: /\nDisallow: /jobs/", "/jobs/1"))
        self.assertTrue(self.allows("User-agent: *\nDisallow: /jobs/\nAllow: /jobs/open/", "/jobs/open/1"))

    def test_a_tie_goes_to_allow(self):
        self.assertTrue(self.allows("User-agent: *\nDisallow: /a\nAllow: /a", "/a"))

    def test_wildcards_and_line_ends(self):
        self.assertFalse(self.allows("User-agent: *\nDisallow: /*.xml$", "/feeds/jobs.xml"))
        self.assertTrue(self.allows("User-agent: *\nDisallow: /*.xml$", "/feeds/jobs.xml.html"))

    def test_a_group_naming_gus_replaces_the_general_one(self):
        text = "User-agent: *\nDisallow: /a\n\nUser-agent: gus\nDisallow: /b\n"
        self.assertTrue(self.allows(text, "/a"))
        self.assertFalse(self.allows(text, "/b"))

    def test_a_group_for_someone_else_doesnt_apply(self):
        self.assertTrue(self.allows("User-agent: googlebot\nDisallow: /\n", "/a"))

    def test_shared_agent_lines_and_an_empty_disallow(self):
        self.assertFalse(self.allows("User-agent: a\nUser-agent: *\nDisallow: /x\n", "/x"))
        self.assertTrue(self.allows("User-agent: *\nDisallow:\n", "/x"))


class Fetching(Base):
    def test_a_missing_robots_file_means_no_rules(self):
        with self.fetch(Site({"example.com/page": b"hello"})):
            self.assertEqual(scan.polite_fetch("https://example.com/page"), "hello")

    def test_a_refused_or_broken_robots_file_stops_everything(self):
        for code in (401, 403, 429, 503):
            scan._robots.clear()
            site = Site({"robots.txt": refused("x", code), "/page": b"hello"})
            with self.fetch(site), self.assertRaises(scan.Refused):
                scan.polite_fetch("https://example.com/page")
            self.assertEqual(len(site.asked), 1)

    def test_a_disallowed_page_is_never_requested(self):
        site = Site({"robots.txt": b"User-agent: *\nDisallow: /private/\n"})
        with self.fetch(site), self.assertRaises(scan.Refused):
            scan.polite_fetch("https://example.com/private/1")
        self.assertEqual(len(site.asked), 1)

    def test_robots_is_read_once_per_host(self):
        site = Site({"robots.txt": b"", "/a": b"a", "/b": b"b"})
        with self.fetch(site):
            scan.polite_fetch("https://example.com/a")
            scan.polite_fetch("https://example.com/b")
        self.assertEqual(sum("robots" in u for u in site.asked), 1)

    def test_only_public_web_addresses(self):
        site = Site({})
        for url in ("http://example.com/a", "https://127.0.0.1/a", "https://localhost/a", "https://sipi.local/a", "https://example.com:8000/a", "file:///etc/passwd"):
            with self.fetch(site), self.assertRaises(scan.Refused):
                scan.polite_fetch(url)
        self.assertEqual(site.asked, [])

    def test_a_sitemap_index_is_followed_on_the_same_host_only(self):
        index = (b'<sitemapindex><sitemap><loc>https://example.com/a.xml</loc></sitemap>'
                 b'<sitemap><loc>https://elsewhere.com/b.xml</loc></sitemap></sitemapindex>')
        site = Site({"robots.txt": b"", "/sitemap.xml": index, "/a.xml": b"<urlset><url><loc>https://example.com/job/x/1/</loc></url></urlset>"})
        with self.fetch(site):
            self.assertEqual(scan.sitemap_urls("https://example.com/sitemap.xml"), ["https://example.com/job/x/1/"])
        self.assertFalse(any("elsewhere" in u for u in site.asked))

    def test_a_bot_check_instead_of_a_sitemap_is_a_refusal(self):
        with self.fetch(Site({"robots.txt": b"", "/sitemap.xml": b"<html>Just a moment...</html>"})), self.assertRaises(scan.Refused):
            scan.sitemap_urls("https://example.com/sitemap.xml")


class Addresses(unittest.TestCase):
    def test_successfactors(self):
        self.assertEqual(scan.successfactors_job("https://careers.optimizely.com/job/Dhaka-Product-Manager-1212-BD/1360527755/"),
                         ("1360527755", "Dhaka Product Manager 1212 BD"))
        self.assertEqual(scan.successfactors_job("https://careers.ey.com/ey/job/New-York-VP%2C-GTM-10003/1375716455/"), ("1375716455", "New York VP, GTM 10003"))
        self.assertIsNone(scan.successfactors_job("https://careers.optimizely.com/go/Engineering/123/"))

    def test_workday(self):
        self.assertEqual(scan.workday_job(f"https://{WD_HOST}/External_Career_Site/job/United-Kingdom---London/Principal-AI-Architect_JR362633"),
                         ("JR362633", "Principal AI Architect"))
        self.assertEqual(scan.workday_job(f"https://{WD_HOST}/S/job/Director--Performance_JR367588-1"), ("JR367588-1", "Director Performance"))


class Pages(unittest.TestCase):
    def test_successfactors_page(self):
        row = scan.successfactors_page(sample("successfactors_job.html").decode())
        self.assertEqual((row["title"], row["location"], row["posted"]), ("Product Manager", "Dhaka, BD, 1212 BD", "2026-10-10"))
        self.assertEqual(row["description"], "Own the roadmap for our platform. Work with design & engineering.")

    def test_britain_is_named_so_location_rules_can_match(self):
        text = sample("successfactors_job.html").decode().replace("Dhaka, BD, 1212 BD", "Bristol, GB, BS1 4QA")
        self.assertEqual(scan.successfactors_page(text)["location"], "Bristol, United Kingdom, BS1 4QA")

    def test_workday_page_gives_every_place(self):
        row = scan.workday_page(sample("workday_job.json").decode())
        self.assertEqual((row["title"], row["posted"]), ("Senior Product Manager", "2026-11-06"))
        self.assertEqual(row["location"], "California - San Francisco / United Kingdom - London")
        self.assertEqual(row["description"], "Own the roadmap for Agentforce.")

    def test_expired_ads_hold_no_job(self):
        self.assertIsNone(scan.successfactors_page(sample("successfactors_gone.html").decode()))
        self.assertIsNone(scan.workday_page('{"jobPostingInfo": {"posted": false}}'))
        self.assertIsNone(scan.workday_page('{"errorCode": "S21"}'))

    def test_a_job_address_points_at_its_data(self):
        self.assertEqual(scan.workday_job_data(f"https://{WD_HOST}/External_Career_Site/job/UK---London/PM_JR1"),
                         f"https://{WD_HOST}/wday/cxs/salesforce/External_Career_Site/job/UK---London/PM_JR1")


class Optimizely(Base):
    def setUp(self):
        super().setUp()
        self.watch(["Optimizely", "successfactors", SF, "x"])

    def test_finds_its_roles_and_adds_none(self):
        """It has product managers elsewhere and none in London, so a correct scan reads them and saves nothing."""
        jobs, errors = self.run_scan(successfactors_site())
        self.assertEqual(errors, {})
        self.assertEqual([j["title"] for j in jobs], ["Product Manager"] * 3)
        cfg = scan.load_config()
        self.assertFalse([j for j in jobs if scan.matches(j, cfg)])

    def test_reads_job_pages_only_for_wanted_titles(self):
        site = successfactors_site()
        self.run_scan(site)
        self.assertEqual(len(site.pages()), 3)
        self.assertFalse([u for u in site.pages() if "Sales-Development" in u or "Solution-Architect" in u or "Consultant" in u])

    def test_never_touches_the_rss_it_is_told_to_avoid(self):
        site = successfactors_site()
        self.run_scan(site)
        self.assertFalse([u for u in site.asked if "/services/" in u])

    def test_a_role_elsewhere_isnt_read_again(self):
        self.run_scan(successfactors_site())
        site = successfactors_site()
        self.run_scan(site)
        self.assertEqual(site.pages(), [])

    def test_a_london_role_is_kept(self):
        page = sample("successfactors_job.html").replace(b"Dhaka, BD, 1212 BD", b"London, GB, WC2N 4JS")
        jobs, _ = self.run_scan(successfactors_site(**{f"{SF}/job/": page}))
        self.assertTrue(all(scan.matches(j, scan.load_config()) for j in jobs))
        self.assertTrue(all(j["id"].startswith(f"sf:{SF}:") for j in jobs))

    def test_a_saved_role_needs_no_page_and_counts_as_live(self):
        saved = {f"sf:{SF}:1360527755": {"id": f"sf:{SF}:1360527755", "company": "Optimizely", "title": "Product Manager", "location": "London", "description": "", "url": "u"}}
        scan.JOBS.write_text(json.dumps(saved))
        site = successfactors_site()
        jobs, _ = self.run_scan(site)
        self.assertNotIn("1360527755", " ".join(site.pages()))
        self.assertIn(f"sf:{SF}:1360527755", {j["id"] for j in jobs})

    def test_stops_at_the_limit_and_finishes_next_scan(self):
        with mock.patch.object(scan, "PAGES_PER_SCAN", 2):
            first = successfactors_site()
            self.run_scan(first)
            second = successfactors_site()
            self.run_scan(second)
        self.assertEqual((len(first.pages()), len(second.pages())), (2, 1))

    def test_waits_between_pages(self):
        with mock.patch.object(scan, "PAGE_PAUSE", 7), mock.patch.object(scan.time, "sleep") as sleep:
            self.run_scan(successfactors_site())
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0, 7, 7])


class Refusals(Base):
    def setUp(self):
        super().setUp()
        self.watch(["Optimizely", "successfactors", SF, "x"])

    def test_a_blocked_sitemap_is_recorded_and_the_scan_moves_on(self):
        self.watch(["Optimizely", "successfactors", SF, "x"], ["Acme", "recruitee", "acme", "x"])
        site = successfactors_site(**{f"{SF}/sitemap.xml": refused(SF), "acme.recruitee.com": b'{"offers": []}'})
        _, errors = self.run_scan(site)
        self.assertIn("403", errors["Optimizely"])
        self.assertEqual(site.pages(), [])
        self.assertNotIn("Acme", errors)

    def test_a_refused_job_page_stops_that_board_at_once(self):
        site = successfactors_site(**{f"{SF}/job/": refused(SF)})
        _, errors = self.run_scan(site)
        self.assertEqual(len(site.pages()), 1)
        self.assertIn("403", errors["Optimizely"])

    def test_too_many_requests_also_stops_it(self):
        site = successfactors_site(**{f"{SF}/job/": refused(SF, 429)})
        self.run_scan(site)
        self.assertEqual(len(site.pages()), 1)

    def test_a_bot_check_on_a_job_page_stops_that_board(self):
        site = successfactors_site(**{f"{SF}/job/": b"<html>Please complete the CAPTCHA</html>"})
        _, errors = self.run_scan(site)
        self.assertEqual(len(site.pages()), 1)
        self.assertIn("bot check", errors["Optimizely"])

    def test_robots_ruling_out_job_pages_means_none_are_fetched(self):
        site = successfactors_site(**{f"{SF}/robots.txt": b"User-agent: *\nDisallow: /job/\n"})
        _, errors = self.run_scan(site)
        self.assertEqual(site.pages(), [])
        self.assertIn("robots.txt disallows", errors["Optimizely"])

    def test_robots_ruling_out_the_sitemap_means_nothing_is_fetched(self):
        site = successfactors_site(**{f"{SF}/robots.txt": b"User-agent: *\nDisallow: /\n"})
        _, errors = self.run_scan(site)
        self.assertEqual(len(site.asked), 1)
        self.assertIn("robots.txt disallows", errors["Optimizely"])

    def test_an_expired_ad_is_skipped_and_the_rest_are_read(self):
        one = iter([sample("successfactors_gone.html"), sample("successfactors_job.html"), sample("successfactors_job.html")])
        jobs, errors = self.run_scan(successfactors_site(**{f"{SF}/job/": lambda _: next(one)}))
        self.assertEqual((len(jobs), errors), (2, {}))

    def test_a_404_job_page_is_an_expired_ad(self):
        pages = iter([refused(SF, 404), sample("successfactors_job.html"), sample("successfactors_job.html")])
        jobs, errors = self.run_scan(successfactors_site(**{f"{SF}/job/": lambda _: next(pages)}))
        self.assertEqual((len(jobs), errors), (2, {}))

    def test_a_board_whose_pages_cant_be_read_shows_up_as_broken(self):
        _, errors = self.run_scan(successfactors_site(**{f"{SF}/job/": b"<html>a new design</html>"}))
        self.assertIn("could be read", errors["Optimizely"])

    def test_unreadable_pages_keep_the_board_flagged_on_later_scans(self):
        """Once tried, the pages aren't asked for again for a week, but the board must not look healthy and empty meanwhile."""
        bad = {f"{SF}/job/": b"<html>a new design</html>"}
        self.run_scan(successfactors_site(**bad))
        site = successfactors_site(**bad)
        jobs, errors = self.run_scan(site)
        self.assertEqual(site.pages(), [])
        self.assertIn("could be read", errors["Optimizely"])

    def test_an_unreadable_page_is_tried_again_after_a_week(self):
        pages = iter([b"<html>a one-off glitch</html>", sample("successfactors_job.html"), sample("successfactors_job.html")])
        self.run_scan(successfactors_site(**{f"{SF}/job/": lambda _: next(pages)}))
        soon = successfactors_site()
        self.run_scan(soon)
        self.assertEqual(soon.pages(), [])
        seen = json.loads(scan.LOOKED_UP.read_text())
        for entry in seen.values():
            if entry.get("unread"):
                entry["unread"] = "2020-01-01"
        scan.LOOKED_UP.write_text(json.dumps(seen))
        page = sample("successfactors_job.html").replace(b"Dhaka, BD, 1212 BD", b"London, GB, WC2N 4JS")
        later = successfactors_site(**{f"{SF}/job/": page})
        jobs, errors = self.run_scan(later)
        self.assertEqual((len(later.pages()), len(jobs), errors), (1, 1, {}))

    def test_one_expired_ad_alone_is_no_alarm(self):
        """The only new page this scan is an ad that has just closed: the board is fine."""
        for gone in (refused(SF, 404), refused(SF, 410), sample("successfactors_gone.html")):
            self.clear()
            scan.LOOKED_UP.unlink(missing_ok=True)
            one = b"<urlset><url><loc>https://careers.optimizely.com/job/London-Product-Manager/1/</loc></url></urlset>"
            jobs, errors = self.run_scan(successfactors_site(**{f"{SF}/sitemap.xml": one, f"{SF}/job/": gone}))
            self.assertEqual((jobs, errors), ([], {}))

    def test_ads_the_site_says_are_gone_never_count_against_the_board(self):
        site = successfactors_site(**{f"{SF}/job/": refused(SF, 404)})
        jobs, errors = self.run_scan(site)
        self.assertEqual((len(site.pages()), jobs, errors), (3, [], {}))
        again = successfactors_site(**{f"{SF}/job/": refused(SF, 404)})
        self.assertEqual(self.run_scan(again)[1], {})
        self.assertEqual(again.pages(), [])

    def test_unreadable_pages_beside_readable_ones_are_no_alarm(self):
        pages = iter([b"<html>odd</html>", b"<html>odd</html>", sample("successfactors_job.html")])
        jobs, errors = self.run_scan(successfactors_site(**{f"{SF}/job/": lambda _: next(pages)}))
        self.assertEqual((len(jobs), errors), (1, {}))

    def test_a_sitemap_with_no_jobs_is_an_error_not_an_empty_board(self):
        _, errors = self.run_scan(successfactors_site(**{f"{SF}/sitemap.xml": b"<urlset></urlset>"}))
        self.assertIn("no jobs", errors["Optimizely"])


class Closing(Base):
    def saved(self, jid):
        return {jid: {"id": jid, "company": "A", "title": "Product Manager", "location": "London", "description": "", "url": "u"}}

    def scan_twice(self, jid, site_for):
        scan.JOBS.write_text(json.dumps(self.saved(jid)))
        for _ in range(2):
            self.run_scan(site_for())
            store = json.loads(scan.JOBS.read_text())
            scan.mark_closed(store, scan._boards_answered, set(scan._listed), "now")
            scan.JOBS.write_text(json.dumps(store))
        return store[jid]

    def test_successfactors_lists_everything_so_a_missing_role_closes(self):
        self.watch(["Optimizely", "successfactors", SF, "x"])
        self.assertEqual(self.scan_twice(f"sf:{SF}:1", successfactors_site).get("ad_closed"), "now")

    def test_a_listed_successfactors_role_stays_open(self):
        self.watch(["Optimizely", "successfactors", SF, "x"])
        self.assertIsNone(self.scan_twice(f"sf:{SF}:1360527755", successfactors_site).get("ad_closed"))

    def test_a_refused_board_closes_nothing(self):
        self.watch(["Optimizely", "successfactors", SF, "x"])
        site_for = lambda: successfactors_site(**{f"{SF}/sitemap.xml": refused(SF)})
        self.assertEqual(self.scan_twice(f"sf:{SF}:1", site_for).get("missed"), None)

    def workday_twice(self, site_for):
        """A saved role that isn't in the list for your locations."""
        self.watch(["Salesforce", "workday", WD, "x"])
        jid = f"wd:{WD}:JR1"
        role = {**self.saved(jid)[jid], "url": f"https://{WD_HOST}/External_Career_Site/job/Texas---Austin/Product-Manager_JR1"}
        with mock.patch.object(self, "saved", lambda _: {jid: role}):
            return self.scan_twice(jid, site_for)

    def test_a_saved_workday_role_outside_the_list_stays_open_while_its_page_exists(self):
        self.assertIsNone(self.workday_twice(workday_site).get("ad_closed"))

    def test_a_saved_workday_role_closes_when_its_page_has_gone(self):
        site_for = lambda: workday_site(**{"Product-Manager_JR1": refused("x", 404)})
        self.assertEqual(self.workday_twice(site_for).get("ad_closed"), "now")

    def test_saved_roles_beyond_the_check_limit_stay_open(self):
        with mock.patch.object(scan, "SAVED_CHECKS", 0):
            role = self.workday_twice(lambda: workday_site(**{"Product-Manager_JR1": refused("x", 404)}))
        self.assertIsNone(role.get("ad_closed"))

    def test_a_list_cut_short_closes_nothing(self):
        with mock.patch.object(scan, "WORKDAY_LIST_PAGES", 1):
            role = self.workday_twice(lambda: workday_site(**{"Product-Manager_JR1": refused("x", 404)}))
        self.assertIsNone(role.get("ad_closed"))
        self.assertIsNone(role.get("missed"))


class Salesforce(Base):
    def setUp(self):
        super().setUp()
        self.watch(["Salesforce", "workday", WD, "x"])

    def test_keeps_a_role_whose_second_place_is_london(self):
        jobs, errors = self.run_scan(workday_site())
        self.assertEqual(errors, {})
        self.assertEqual(jobs[0]["id"], f"wd:{WD}:JR360001")
        self.assertTrue(scan.matches(jobs[0], scan.load_config()))
        self.assertEqual(jobs[0]["url"], f"https://{WD_HOST}/External_Career_Site/job/United-Kingdom---London/Senior-Product-Manager_JR360001")

    def test_reads_details_only_for_wanted_titles(self):
        site = workday_site()
        self.run_scan(site)
        self.assertEqual([u.rsplit("/", 1)[1] for u in site.pages()], ["Senior-Product-Manager_JR360001"])

    def test_asks_only_for_jobs_in_your_locations(self):
        site = workday_site()
        self.run_scan(site)
        country, places = site.filters()[1:]
        self.assertEqual(site.filters()[0], {})
        self.assertEqual(list(country.values()), [["29247e57dbaf46fb855b224e03170bc7"]])  # United Kingdom, not India
        self.assertEqual(len(places["locations"]), 2)  # London and England remote, not Bangalore or California remote

    def test_never_filters_by_anything_but_place(self):
        site = workday_site()
        self.run_scan(site)
        self.assertFalse([f for f in site.filters() if "jobFamilyGroup" in f])

    def test_a_board_with_nothing_in_your_locations_takes_one_request(self):
        board = json.loads(sample("workday_list.json"))
        board["facets"] = [{"facetParameter": "locations", "descriptor": "Locations", "values": [{"descriptor": "India - Bangalore", "id": "a", "count": 9}]}]
        site = workday_site(**{"External_Career_Site/jobs": json.dumps(board).encode()})
        jobs, errors = self.run_scan(site)
        self.assertEqual((len(site.lists()), jobs, errors), (1, [], {}))

    def test_a_board_with_no_location_filter_is_reported_not_read_in_full(self):
        board = {**json.loads(sample("workday_list.json")), "facets": [{"facetParameter": "jobFamilyGroup", "descriptor": "Job Category", "values": [{"descriptor": "Product", "id": "a"}]}]}
        site = workday_site(**{"External_Career_Site/jobs": json.dumps(board).encode()})
        _, errors = self.run_scan(site)
        self.assertEqual(len(site.lists()), 1)
        self.assertIn("no location filter", errors["Salesforce"])

    def test_goes_through_every_page_of_a_filtered_list(self):
        board = json.loads(sample("workday_list.json"))
        board["facets"] = board["facets"][:1]
        first = json.dumps(board).encode()
        pages = iter([first, json.dumps({"total": 21, "jobPostings": board["jobPostings"] * 5}).encode(), json.dumps({"total": 0, "jobPostings": board["jobPostings"][:1]}).encode()])
        site = workday_site(**{"External_Career_Site/jobs": lambda _: next(pages)})
        self.run_scan(site)
        self.assertEqual([json.loads(b)["offset"] for b in site.bodies if b], [0, 0, 20])

    def test_an_expired_ad_is_skipped(self):
        jobs, _ = self.run_scan(workday_site(**{"External_Career_Site/job/": refused("x", 404)}))
        self.assertEqual(jobs, [])

    def test_robots_ruling_out_the_data_stops_the_board(self):
        site = workday_site(**{f"{WD_HOST}/robots.txt": b"User-agent: *\nDisallow: /wday/\n"})
        _, errors = self.run_scan(site)
        self.assertEqual(len(site.asked), 1)
        self.assertIn("robots.txt disallows", errors["Salesforce"])

    def test_a_refusal_stops_the_list_at_once(self):
        site = workday_site(**{"External_Career_Site/jobs": refused("x")})
        _, errors = self.run_scan(site)
        self.assertEqual(len(site.lists()), 1)
        self.assertIn("403", errors["Salesforce"])

    def test_a_bot_check_instead_of_data_stops_the_board(self):
        site = workday_site(**{"External_Career_Site/jobs": b"<html>Just a moment...</html>"})
        _, errors = self.run_scan(site)
        self.assertEqual(site.pages(), [])
        self.assertIn("Salesforce", errors)


class Resolving(Base):
    def test_careers_address_on_successfactors(self):
        with self.fetch(successfactors_site()):
            self.assertEqual(resolve.from_careers(f"https://{SF}/"), ("successfactors", SF, 6))

    def test_a_workday_address_names_its_site(self):
        with self.fetch(workday_site()):
            self.assertEqual(resolve.from_careers(f"https://{WD_HOST}/en-US/External_Career_Site/job/x"), ("workday", WD, 4))

    def test_a_workday_address_with_no_site_uses_the_first_robots_lists(self):
        with self.fetch(workday_site()):
            self.assertEqual(resolve.from_careers(f"https://{WD_HOST}/"), ("workday", WD, 4))

    def test_a_careers_page_that_links_to_a_board(self):
        page = f'<a href="https://{WD_HOST}/External_Career_Site">Open roles</a>'.encode()
        site = workday_site(**{"www.acme.com/careers": page, "www.acme.com/robots.txt": b""})
        with self.fetch(site):
            self.assertEqual(resolve.from_careers("https://www.acme.com/careers"), ("workday", WD, 4))

    def test_a_careers_page_that_links_to_a_careers_host(self):
        page = f'<a href="https://{SF}/go/all">Open roles</a>'.encode()
        site = successfactors_site(**{"www.acme.com/careers": page, "www.acme.com/robots.txt": b""})
        with self.fetch(site):
            self.assertEqual(resolve.from_careers("https://www.acme.com/careers"), ("successfactors", SF, 6))

    def test_a_board_address_is_checked_through_its_api(self):
        with mock.patch.object(resolve, "probe", return_value=12) as probe:
            self.assertEqual(resolve.from_careers("https://jobs.ashbyhq.com/acme"), ("ashby", "acme", 12))
        probe.assert_called_once_with("ashby", "acme")

    def test_a_sitemap_of_other_pages_isnt_a_board(self):
        site = Site({"robots.txt": b"", "/sitemap.xml": b"<urlset><url><loc>https://careers.acme.com/about</loc></url></urlset>"})
        with self.fetch(site):
            self.assertIsNone(resolve.probe("successfactors", "careers.acme.com"))

    def test_a_refusal_means_no_board(self):
        with self.fetch(successfactors_site(**{f"{SF}/sitemap.xml": refused(SF)})):
            self.assertIsNone(resolve.probe("successfactors", SF))
        scan._robots.clear()
        with self.fetch(Site({"robots.txt": refused(SF)})):
            self.assertIsNone(resolve.try_board("workday", "salesforce.wd12"))

    def test_a_careers_address_beats_any_guess(self):
        with mock.patch.object(resolve, "from_careers", return_value=("workday", WD, 100)), mock.patch.object(resolve, "try_board") as tried:
            self.assertEqual(resolve.resolve("Acme", "https://acme.com/careers"), ("workday", WD, 100))
        tried.assert_not_called()

    def test_a_name_alone_never_guesses_at_enterprise_boards(self):
        tried = []
        with mock.patch.object(resolve, "try_board", lambda ats, slug: tried.append(ats)):
            resolve.resolve("Acme")
        self.assertTrue(tried)
        self.assertFalse(set(tried) & set(scan.SITE_BOARDS))

    def test_missing_lists_active_companies_with_no_board(self):
        (self.dir / "companies.json").write_text(json.dumps({
            "A": {"status": "maybe", "careers_url": "https://a.com/careers"}, "B": {"status": "watched", "board": "ashby:b"},
            "C": {"status": "rejected"}, "D": {"status": "candidate", "board": None}}))
        with mock.patch.object(resolve, "DATA", self.dir):
            self.assertEqual(resolve.missing(), {"A": "https://a.com/careers", "D": None})


if __name__ == "__main__":
    unittest.main()
