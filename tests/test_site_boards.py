"""Company careers sites: read only what each site publishes, take no for an answer. No network: replies are saved samples."""
import csv
import json
import re
import sys
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
TT = "acme-1721317262.teamtailor.com"
GEM_API = "api.gem.com/job_board/v0/acme/job_posts/"


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


def teamtailor_site(host=TT, **more):
    return Site({f"{host}/robots.txt": sample("teamtailor_robots.txt"), f"{host}/jobs.json": sample("teamtailor_feed.json")}, **more)


def gateway_403(url):
    """What Gem's API answers for every address it doesn't know, /robots.txt included."""
    return urllib.error.HTTPError(url, 403, "Forbidden", {"x-amzn-ErrorType": "ForbiddenException"}, None)


def gem_site(**more):
    return Site({"api.gem.com/robots.txt": gateway_403, GEM_API: sample("gem_job_posts.json")}, **more)


def feed(*items):
    return json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": list(items)}).encode()


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
        for url in ("http://example.com/a", "https://127.0.0.1/a", "https://localhost/a", "https://pi.local/a", "https://example.com:8000/a", "file:///etc/passwd"):
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


GOOGLE = "https://www.google.com/about/careers/applications/jobs/results"
GOOGLE_PAGE = "/jobs/results?"
SEARCH = f"{GOOGLE}?location=London%2C%20UK&q=%22product%20manager%22&company=Google&company=DeepMind"
ROBOTS = "www.google.com/robots.txt"


def google_site(**more):
    return Site({ROBOTS: sample("google_robots.txt"), GOOGLE_PAGE: sample("google_careers_results.html")}, **more)


def google_page(rows, total=None):
    data = json.dumps([rows, None, len(rows) if total is None else total, 20])
    return f"<script>AF_initDataCallback({{key: 'ds:1', hash: '2', data:{data}, sideChannel: {{}}}});</script>".encode()


class GoogleCareers(Base):
    def setUp(self):
        super().setUp()
        self.watch()
        self.search = SEARCH

    def run_scan(self, site):
        real = scan.load_config

        def with_search():
            return {**real(), "google_careers_search": self.search}
        with mock.patch.object(scan, "load_config", with_search):
            return super().run_scan(site)

    def state(self):
        return json.loads((self.dir / "google_careers.json").read_text())

    def a_day_later(self):
        state = {**self.state(), "read": self.state()["read"] - scan.GOOGLE_EVERY - 1}
        (self.dir / "google_careers.json").write_text(json.dumps(state))

    def pages(self, site):
        return [u for u in site.asked if "robots" not in u]

    def role(self, jobs, title):
        return next(j for j in jobs if j["title"] == title)

    def test_one_page_is_asked_for_newest_first(self):
        site = google_site()
        jobs, errors = self.run_scan(site)
        self.assertEqual(errors, {})
        self.assertEqual(len(jobs), 4)
        self.assertEqual(self.pages(site), [f"{SEARCH}&sort_by=date"])

    def test_a_page_number_in_the_saved_search_is_dropped(self):
        self.search = f"{SEARCH}&page=3&sort_by=relevance"
        site = google_site()
        self.run_scan(site)
        self.assertEqual(self.pages(site), [f"{SEARCH}&sort_by=date"])

    def test_it_reads_the_real_fields_and_every_place(self):
        jobs, _ = self.run_scan(google_site())
        job = self.role(jobs, "Senior Product Manager, AI Ecosystems, Frontier AI, DeepMind")
        self.assertEqual(job["id"], "gc:search:129187054603903686")
        self.assertEqual(job["company"], "Google DeepMind")
        self.assertEqual(job["location"], "Paris, France / Zürich, Switzerland / London, UK / Mountain View, CA, USA")
        self.assertEqual(job["url"], f"{GOOGLE}/129187054603903686-senior-product-manager-ai-ecosystems-frontier-ai-deepmind")
        self.assertTrue(job["posted"].startswith("2026-11-03"))
        self.assertTrue(job["description"])
        self.assertNotIn("<", job["description"])

    def test_a_title_with_a_slash_keeps_the_link_google_gave_it(self):
        jobs, _ = self.run_scan(google_site())
        self.assertTrue(self.role(jobs, "Forward Deployed Engineering Intern, BS/MS, 2027")["url"].endswith("-forward-deployed-engineering-intern-bsms-2027"))

    def test_it_reads_once_a_day_however_often_scans_run(self):
        site = google_site()
        self.run_scan(site)
        jobs, errors = self.run_scan(site)
        self.assertEqual((jobs, errors, len(self.pages(site))), ([], {}, 1))
        self.a_day_later()
        self.run_scan(site)
        self.assertEqual(len(self.pages(site)), 2)

    def test_a_scan_that_reads_nothing_closes_nothing(self):
        self.run_scan(google_site())
        self.run_scan(google_site())
        self.assertNotIn("gc:search", scan._boards_answered)

    def test_changing_the_search_reads_it_straight_away(self):
        site = google_site()
        self.run_scan(site)
        self.search = f"{GOOGLE}?q=lead"
        self.run_scan(site)
        self.assertEqual(self.pages(site)[1], f"{GOOGLE}?q=lead&sort_by=date")

    def test_a_short_list_can_show_a_role_has_gone(self):
        self.run_scan(google_site())
        self.assertIn("gc:search", scan._boards_answered)

    def test_a_list_longer_than_a_page_proves_nothing_has_gone(self):
        jobs, _ = self.run_scan(google_site())
        self.assertEqual(len(jobs), 4)
        self.a_day_later()
        rows = json.loads(re.search(r"data:(\[\[\[\"\d.*?), sideChannel", sample("google_careers_results.html").decode(), re.S)[1])[0]
        self.run_scan(google_site(**{GOOGLE_PAGE: google_page(rows, total=25)}))
        self.assertNotIn("gc:search", scan._boards_answered)

    def test_a_search_with_no_results_is_not_an_error(self):
        jobs, errors = self.run_scan(google_site(**{GOOGLE_PAGE: google_page(None, total=0)}))
        self.assertEqual((jobs, errors), ([], {}))
        self.assertIn("gc:search", scan._boards_answered)

    def test_an_address_robots_disallows_is_never_fetched(self):
        site = google_site(**{ROBOTS: b"User-agent: *\nDisallow: /about/careers/\n"})
        _, errors = self.run_scan(site)
        self.assertEqual(self.pages(site), [])
        self.assertIn("robots.txt disallows", errors["Google"])

    def test_a_refused_request_stays_on_health_until_the_next_read(self):
        site = google_site(**{GOOGLE_PAGE: refused(GOOGLE, 429)})
        _, errors = self.run_scan(site)
        self.assertIn("429", errors["Google"])
        _, errors = self.run_scan(site)
        self.assertIn("429", errors["Google"])
        self.assertEqual(len(self.pages(site)), 1)

    def test_a_bot_check_or_a_changed_page_is_reported_not_read_as_empty(self):
        _, errors = self.run_scan(google_site(**{GOOGLE_PAGE: b"<html>captcha</html>"}))
        self.assertIn("bot check", errors["Google"])
        self.a_day_later()
        _, errors = self.run_scan(google_site(**{GOOGLE_PAGE: b"<html>new design</html>"}))
        self.assertIn("no longer has job results", errors["Google"])
        self.a_day_later()
        _, errors = self.run_scan(google_site(**{GOOGLE_PAGE: google_page([["1", "PM"]])}))
        self.assertIn("no longer has job results", errors["Google"])

    def test_another_address_is_refused_without_a_request(self):
        self.search = "https://www.google.com/search?q=jobs"
        site = google_site()
        _, errors = self.run_scan(site)
        self.assertEqual(site.asked, [])
        self.assertIn("isn't a search", errors["Google"])

    def test_no_search_saved_means_no_requests(self):
        self.search = ""
        site = google_site()
        self.assertEqual(self.run_scan(site), ([], {}))
        self.assertEqual(site.asked, [])

    def test_a_role_already_found_elsewhere_is_not_added_twice(self):
        (self.dir / "jobs.json").write_text(json.dumps({"adzuna:1": {
            "id": "adzuna:1", "company": "Google", "title": "Senior Product Manager, AI Ecosystems, Frontier AI, DeepMind", "location": "London"}}))
        real = scan.load_config

        def with_search():
            return {**real(), "google_careers_search": self.search, "source_hn": False}
        with self.fetch(google_site()), mock.patch.object(scan, "load_config", with_search), mock.patch.object(sys, "argv", ["scan"]):
            scan.main()
        store = json.loads((self.dir / "jobs.json").read_text())
        self.assertNotIn("gc:search:129187054603903686", store)
        self.assertIn("gc:search:88601134075126470", store)


class Teamtailor(Base):
    def setUp(self):
        super().setUp()
        self.watch(["Acme", "teamtailor", TT, "x"])

    def role(self, jobs, title="Senior Product Manager"):
        return next(j for j in jobs if j["title"] == title)

    def test_one_request_reads_the_whole_board(self):
        site = teamtailor_site()
        jobs, errors = self.run_scan(site)
        self.assertEqual(errors, {})
        self.assertEqual(len(jobs), 4)
        self.assertEqual([u for u in site.asked if "robots" not in u], [f"https://{TT}/jobs.json"])

    def test_it_reads_the_real_fields_and_every_place(self):
        jobs, _ = self.run_scan(teamtailor_site())
        job = self.role(jobs)
        self.assertEqual(job["id"], f"tt:{TT}:4632288f-4964-4533-b081-a58e5c113215")
        self.assertEqual(job["location"], "Paris, France / London, United Kingdom")
        self.assertEqual(job["url"], f"https://{TT}/jobs/8460685-senior-product-manager")
        self.assertEqual(job["posted"], "2026-09-26T14:34:12+02:00")
        self.assertIn("own a core area of the platform & work", job["description"])
        self.assertNotIn("<", job["description"])

    def test_a_role_in_two_cities_matches_on_either(self):
        jobs, _ = self.run_scan(teamtailor_site())
        cfg = {**scan.load_config(), "title_include": re.compile("product manager", re.I), "location_include": re.compile("London", re.I)}
        self.assertTrue(scan.matches(self.role(jobs), cfg))

    def test_a_remote_role_is_marked_without_the_country_the_feed_gives(self):
        jobs, _ = self.run_scan(teamtailor_site())
        self.assertEqual(self.role(jobs, "Product Manager, Data")["location"], "Stockholm, Sweden / Remote")

    def test_a_place_named_after_its_country_is_not_repeated(self):
        job = {"jobLocation": [{"address": {"addressLocality": "United States", "addressCountry": "US"}}]}
        self.assertEqual(scan.teamtailor_place(job), "United States")

    def test_a_job_with_no_place_is_kept_without_faulting_the_board(self):
        jobs, errors = self.run_scan(teamtailor_site(**{f"{TT}/jobs.json": feed({"id": "1", "title": "PM", "url": "https://x.io/jobs/1"})}))
        self.assertEqual((jobs[0]["location"], errors), ("", {}))

    def test_a_feed_that_stops_giving_places_is_reported(self):
        items = [{"id": str(n), "title": "PM", "url": f"https://x.io/jobs/{n}"} for n in range(3)]
        _, errors = self.run_scan(teamtailor_site(**{f"{TT}/jobs.json": feed(*items)}))
        self.assertIn("no longer says where", errors["Acme"])

    def test_an_address_it_disallows_is_never_fetched(self):
        site = teamtailor_site(**{f"{TT}/robots.txt": b"User-agent: *\nDisallow: /jobs.json\n"})
        _, errors = self.run_scan(site)
        self.assertIn("robots.txt disallows", errors["Acme"])
        self.assertEqual([u for u in site.asked if "robots" not in u], [])

    def test_a_refusal_is_recorded_once_and_the_scan_moves_on(self):
        for code in (403, 429):
            with self.subTest(code):
                site = teamtailor_site(**{f"{TT}/jobs.json": refused(TT, code)})
                _, errors = self.run_scan(site)
                self.assertIn(str(code), errors["Acme"])
                self.assertEqual(len([u for u in site.asked if "jobs.json" in u]), 1)

    def test_a_bot_check_is_reported_as_one(self):
        _, errors = self.run_scan(teamtailor_site(**{f"{TT}/jobs.json": b"<html>Just a moment...</html>"}))
        self.assertIn("bot check", errors["Acme"])

    def test_a_page_that_is_no_feed_is_reported(self):
        for reply in (b"<html>a new design</html>", b"[]", b'{"version": "https://jsonfeed.org/version/1.1"}'):
            with self.subTest(reply):
                _, errors = self.run_scan(teamtailor_site(**{f"{TT}/jobs.json": reply}))
                self.assertIn("no Teamtailor job feed", errors["Acme"])

    def saved(self, **more):
        """One saved role on the board, then a scan that no longer lists it."""
        jid = f"tt:{TT}:gone"
        (self.dir / "jobs.json").write_text(json.dumps({jid: {"id": jid, "company": "Acme", "title": "PM", "location": "London", **more}}))
        return jid

    def scan_twice(self, site):
        """Two full scans, as closing a role takes two misses, then what's saved."""
        cfg = {**scan.load_config(), "source_hn": False}
        for _ in range(2):
            self.clear()
            with self.fetch(site), mock.patch.object(scan, "load_config", lambda: cfg), mock.patch.object(sys, "argv", ["scan"]):
                scan.main()
        return json.loads((self.dir / "jobs.json").read_text())

    def test_a_role_gone_from_the_feed_closes_after_two_scans(self):
        jid = self.saved()
        self.assertTrue(self.scan_twice(teamtailor_site())[jid].get("ad_closed"))

    def test_an_empty_feed_is_a_real_answer(self):
        jid = self.saved()
        self.assertTrue(self.scan_twice(teamtailor_site(**{f"{TT}/jobs.json": feed()}))[jid].get("ad_closed"))

    def test_a_refused_board_closes_nothing(self):
        jid = self.saved()
        self.assertFalse(self.scan_twice(teamtailor_site(**{f"{TT}/jobs.json": refused(TT)}))[jid].get("ad_closed"))

    def test_an_unreadable_ad_closes_nothing_and_does_not_fault_the_board(self):
        jid = self.saved()
        good = json.loads(sample("teamtailor_feed.json"))["items"]
        site = teamtailor_site(**{f"{TT}/jobs.json": feed(*good, {"title": "No id"})})
        self.assertFalse(self.scan_twice(site)[jid].get("ad_closed"))
        self.assertEqual(json.loads((self.dir / "board_errors.json").read_text()), {})

    def test_the_address_on_a_teamtailor_host_is_found_whatever_its_number(self):
        with self.fetch(teamtailor_site()):
            self.assertEqual(resolve.from_careers(f"https://{TT}/jobs/8460685-senior-product-manager"), ("teamtailor", TT, 4))

    def test_a_company_domain_on_teamtailor_is_found(self):
        with self.fetch(teamtailor_site("careers.acme.com")):
            self.assertEqual(resolve.from_careers("https://careers.acme.com/jobs"), ("teamtailor", "careers.acme.com", 4))

    def test_a_page_that_links_to_a_board_is_followed(self):
        page = f'<a href="https://{TT}/jobs">Jobs</a><img src="https://images.teamtailor-cdn.com/x.png">'.encode()
        with self.fetch(teamtailor_site(**{"www.acme.com/careers": page})):
            self.assertEqual(resolve.from_careers("https://www.acme.com/careers"), ("teamtailor", TT, 4))

    def test_a_linked_company_domain_is_tried(self):
        page = b'<a href="https://career.acme.com/">Jobs</a>'
        with self.fetch(teamtailor_site("career.acme.com", **{"www.acme.com/careers": page})):
            self.assertEqual(resolve.from_careers("https://www.acme.com/careers"), ("teamtailor", "career.acme.com", 4))

    def test_teamtailors_own_addresses_are_not_a_company_board(self):
        text = "https://app.teamtailor.com https://www.teamtailor.com https://career.teamtailor.com https://images.teamtailor-cdn.com"
        self.assertEqual(resolve.named_boards(text), [])

    def test_a_board_in_north_america_keeps_its_whole_host(self):
        self.assertEqual(resolve.named_boards("https://acme.na.teamtailor.com/jobs"), [("teamtailor", "acme.na.teamtailor.com")])

    def test_it_is_never_guessed_from_a_name(self):
        site = Site({})
        with self.fetch(site), mock.patch.object(resolve, "get_json", lambda _: {}):
            resolve.resolve("Acme")
        self.assertFalse(any("teamtailor" in u for u in site.asked))


class Gem(Base):
    def setUp(self):
        super().setUp()
        self.watch(["Acme", "gem", "acme", "x"])

    def role(self, jobs, title="Senior Product Manager & Platform Lead"):
        return next(j for j in jobs if j["title"] == title)

    def test_one_request_reads_the_whole_board(self):
        site = gem_site()
        jobs, errors = self.run_scan(site)
        self.assertEqual(errors, {})
        self.assertEqual(len(jobs), 5)
        self.assertEqual([u for u in site.asked if "robots" not in u], [f"https://{GEM_API}"])

    def test_it_reads_the_real_fields(self):
        job = self.role(self.run_scan(gem_site())[0])
        self.assertEqual(job["id"], "gem:acme:am9icG9zdDpAcmVQbGFjZWhvbGRlcjAx")
        self.assertEqual(job["url"], "https://jobs.gem.com/acme/am9icG9zdDpAcmVQbGFjZWhvbGRlcjAx")
        self.assertEqual(job["posted"], "2026-10-08T14:15:12.588Z")
        self.assertIn("core area of the platform & work", job["description"])

    def test_the_description_falls_back_to_the_html(self):
        job = {"id": "1", "title": "PM", "absolute_url": "https://x.io/1", "content": "<p>A &amp; B</p>"}
        self.assertEqual(scan.gem_job("Acme", "acme", job)["description"], "A & B")

    def test_every_office_counts_not_just_the_first(self):
        jobs, _ = self.run_scan(gem_site())
        self.assertEqual(self.role(jobs, "Software Engineer")["location"], "San Francisco, United States / New York, United States")

    def test_an_office_name_the_location_hides_is_kept(self):
        place = self.role(self.run_scan(gem_site())[0])["location"]
        self.assertEqual(place, "London (United Kingdom - Remote) / Prague, Czechia / Dublin (Ireland - Remote)")

    def test_a_city_hidden_in_the_office_name_matches(self):
        jobs, _ = self.run_scan(gem_site())
        cfg = {**scan.load_config(), "title_include": re.compile("product", re.I), "location_include": re.compile("London", re.I)}
        self.assertTrue(scan.matches(self.role(jobs), cfg))

    def test_remote_offices_keep_the_area_they_cover(self):
        jobs, _ = self.run_scan(gem_site())
        self.assertEqual(self.role(jobs, "Head of Product")["location"], "Remote, US (United States - Remote)")
        self.assertEqual(self.role(jobs, "Product Manager, Data")["location"], "Prague, Czechia / Czechia - Remote / Remote Europe (Czechia - Remote) / Brno, Czechia")

    def test_location_type_does_not_overrule_a_remote_place(self):
        self.assertEqual(self.role(self.run_scan(gem_site())[0], "Product Manager, Growth")["location"], "Poland - Remote")

    def test_a_remote_job_naming_no_office_is_marked_remote(self):
        self.assertEqual(scan.gem_place({"location_type": "remote", "offices": []}), "Remote")

    def test_with_no_offices_the_top_level_location_is_used(self):
        self.assertEqual(scan.gem_place({"location": {"name": "London, United Kingdom"}, "offices": []}), "London, United Kingdom")

    def test_a_job_with_no_place_is_kept_without_faulting_the_board(self):
        post = {"id": "1", "title": "PM", "absolute_url": "https://x.io/1"}
        jobs, errors = self.run_scan(gem_site(**{GEM_API: json.dumps([post]).encode()}))
        self.assertEqual((jobs[0]["location"], errors), ("", {}))

    def test_a_list_that_stops_giving_places_is_reported(self):
        posts = [{"id": str(n), "title": "PM", "absolute_url": f"https://x.io/{n}"} for n in range(3)]
        _, errors = self.run_scan(gem_site(**{GEM_API: json.dumps(posts).encode()}))
        self.assertIn("no longer says where", errors["Acme"])

    def test_a_list_of_nothing_but_bad_ads_is_reported(self):
        _, errors = self.run_scan(gem_site(**{GEM_API: b"[1, 2, 3]"}))
        self.assertIn("no longer says where", errors["Acme"])

    def test_a_real_robots_txt_is_obeyed(self):
        site = gem_site(**{"api.gem.com/robots.txt": b"User-agent: *\nDisallow: /job_board/\n"})
        _, errors = self.run_scan(site)
        self.assertIn("robots.txt disallows", errors["Acme"])
        self.assertEqual([u for u in site.asked if "robots" not in u], [])

    def test_any_other_answer_about_robots_txt_stops_the_read(self):
        for code in (401, 403, 429, 503):
            with self.subTest(code):
                site = gem_site(**{"api.gem.com/robots.txt": refused("x", code)})
                _, errors = self.run_scan(site)
                self.assertIn(f"robots.txt answered {code}", errors["Acme"])
                self.assertEqual([u for u in site.asked if "robots" not in u], [])

    def test_the_gateway_403_only_means_no_file_on_gems_api(self):
        site = Site({"elsewhere.example.com/robots.txt": gateway_403})
        with self.fetch(site), self.assertRaises(scan.Refused):
            scan.site_rules("elsewhere.example.com")

    def test_a_refusal_is_recorded_once_and_the_scan_moves_on(self):
        for code in (403, 429):
            with self.subTest(code):
                site = gem_site(**{GEM_API: refused("x", code)})
                _, errors = self.run_scan(site)
                self.assertIn(str(code), errors["Acme"])
                self.assertEqual(len([u for u in site.asked if "job_posts" in u]), 1)

    def test_the_gateway_403_on_the_job_list_is_a_refusal(self):
        _, errors = self.run_scan(gem_site(**{GEM_API: gateway_403}))
        self.assertIn("403", errors["Acme"])

    def test_a_bot_check_is_reported_as_one(self):
        _, errors = self.run_scan(gem_site(**{GEM_API: b"<html>Just a moment...</html>"}))
        self.assertIn("bot check", errors["Acme"])

    def test_a_reply_that_is_no_list_is_reported(self):
        for reply in (b"<html>a new design</html>", b"{}", b'{"jobs": []}'):
            with self.subTest(reply):
                _, errors = self.run_scan(gem_site(**{GEM_API: reply}))
                self.assertIn("no Gem job list", errors["Acme"])

    def saved(self):
        """One saved role on the board, then a scan that no longer lists it."""
        jid = "gem:acme:gone"
        (self.dir / "jobs.json").write_text(json.dumps({jid: {"id": jid, "company": "Acme", "title": "PM", "location": "London"}}))
        return jid

    def scan_twice(self, site):
        """Two full scans, as closing a role takes two misses, then what's saved."""
        cfg = {**scan.load_config(), "source_hn": False}
        for _ in range(2):
            self.clear()
            with self.fetch(site), mock.patch.object(scan, "load_config", lambda: cfg), mock.patch.object(sys, "argv", ["scan"]):
                scan.main()
        return json.loads((self.dir / "jobs.json").read_text())

    def test_a_role_gone_from_the_list_closes_after_two_scans(self):
        jid = self.saved()
        self.assertTrue(self.scan_twice(gem_site())[jid].get("ad_closed"))

    def test_an_empty_list_is_a_real_answer(self):
        jid = self.saved()
        self.assertTrue(self.scan_twice(gem_site(**{GEM_API: b"[]"}))[jid].get("ad_closed"))

    def test_a_refused_board_closes_nothing(self):
        for reply in (refused("x"), refused("x", 404), b"<html>Just a moment...</html>"):
            with self.subTest(reply):
                jid = self.saved()
                self.assertFalse(self.scan_twice(gem_site(**{GEM_API: reply}))[jid].get("ad_closed"))

    def test_a_bad_ad_closes_nothing_and_does_not_fault_the_board(self):
        good = json.loads(sample("gem_job_posts.json"))
        for bad in ({"title": "No id"}, "not an ad", {"id": "1", "title": "", "absolute_url": "https://x.io/1"},
                    {"id": "1", "title": "PM", "absolute_url": "https://x.io/1", "offices": [{"location": "odd"}]}):
            with self.subTest(bad):
                jid = self.saved()
                site = gem_site(**{GEM_API: json.dumps([*good, bad]).encode()})
                saved = self.scan_twice(site)
                self.assertFalse(saved[jid].get("ad_closed"))
                self.assertEqual(json.loads((self.dir / "board_errors.json").read_text()), {})
                (self.dir / "jobs.json").unlink()

    def test_a_bad_ad_is_not_remembered(self):
        good = json.loads(sample("gem_job_posts.json"))
        broken = {**good[0], "title": None}
        self.run_scan(gem_site(**{GEM_API: json.dumps([broken]).encode()}))
        jobs, _ = self.run_scan(gem_site())
        self.assertEqual(self.role(jobs)["id"], f"gem:acme:{good[0]['id']}")

    def test_a_board_address_is_found(self):
        with self.fetch(gem_site()):
            self.assertEqual(resolve.from_careers("https://jobs.gem.com/acme"), ("gem", "acme", 5))

    def test_a_job_address_is_found(self):
        with self.fetch(gem_site()):
            self.assertEqual(resolve.from_careers("https://jobs.gem.com/acme/am9icG9zdDp?src=x"), ("gem", "acme", 5))

    def test_a_page_that_links_to_a_board_is_followed(self):
        page = b'<a href="https://jobs.gem.com/acme">Open roles</a>'
        with self.fetch(gem_site(**{"www.acme.com/careers": page})):
            self.assertEqual(resolve.from_careers("https://www.acme.com/careers"), ("gem", "acme", 5))

    def test_a_page_built_on_the_api_is_followed(self):
        page = b'fetch("https://api.gem.com/job_board/v0/acme/job_posts/")'
        with self.fetch(gem_site(**{"www.acme.com/careers": page})):
            self.assertEqual(resolve.from_careers("https://www.acme.com/careers"), ("gem", "acme", 5))

    def test_gems_own_site_is_not_a_board(self):
        self.assertEqual(resolve.named_boards("https://www.gem.com/pricing https://help.gem.com/x https://static.gem.com/y.js https://myjobs.gem.com/z"), [])

    def test_an_unknown_board_is_not_found(self):
        with self.fetch(gem_site()):
            self.assertIsNone(resolve.from_careers("https://jobs.gem.com/someone-else"))

    def test_it_is_never_guessed_from_a_name(self):
        site = Site({})
        with self.fetch(site), mock.patch.object(resolve, "get_json", lambda _: {}):
            resolve.resolve("Acme")
        self.assertFalse(any("gem.com" in u for u in site.asked))


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


AMAZON = "https://www.amazon.jobs"
AMAZON_SEARCH = f"{AMAZON}/en-gb/search?base_query=product+manager&loc_query=London%2C+England%2C+United+Kingdom&country=GBR&city=London"
AMAZON_JSON = f"{AMAZON}/en-gb/search.json?base_query=product+manager&loc_query=London%2C+England%2C+United+Kingdom&country=GBR&city=London&sort=recent"


def amazon_site(reply=None, **more):
    return Site({"www.amazon.jobs/robots.txt": sample("amazon_robots.txt"), f"{AMAZON}/en-gb/search.json": reply or sample("amazon_jobs_search.json")}, **more)


class AmazonJobs(Base):
    """amazon.jobs isn't a job board system, so a saved search there is read like the Google Careers one: once a day, newest first."""

    def setUp(self):
        super().setUp()
        self.watch()
        self.search = AMAZON_SEARCH

    def run_scan(self, site):
        real = scan.load_config

        def with_search():
            return {**real(), "amazon_jobs_search": self.search}
        with mock.patch.object(scan, "load_config", with_search):
            return super().run_scan(site)

    def pages(self, site):
        return [u for u in site.asked if "robots" not in u]

    def test_the_pasted_search_is_read_as_json_newest_first_with_paging_params_replaced(self):
        self.search = f"{AMAZON_SEARCH}&offset=200&result_limit=10&sort=relevant"
        site = amazon_site()
        jobs, errors = self.run_scan(site)
        self.assertEqual(errors, {})
        self.assertEqual(self.pages(site), [f"{AMAZON_JSON}&result_limit=100&offset=0"])
        self.assertEqual(len(jobs), 3)

    def test_it_reads_the_real_fields_and_the_whole_ad(self):
        jobs, _ = self.run_scan(amazon_site())
        job = next(j for j in jobs if j["id"] == "amz:search:10601122")
        self.assertEqual(job["company"], "Amazon")
        self.assertEqual(job["title"], "Product Manager – Compliance Digital Tools, AWS Enterprise Support")
        self.assertEqual(job["location"], "London, England, GBR")
        self.assertEqual(job["url"], f"{AMAZON}/en-gb/jobs/10601122/product-manager-compliance-digital-tools-aws")
        self.assertEqual(job["posted"], "2026-11-07")
        self.assertIn("Bachelor's degree", job["description"])
        self.assertNotIn("<", job["description"])
        first = next(j for j in jobs if j["id"] == "amz:search:10596386")
        self.assertEqual(first["posted"], "2026-11-05")  # the site's double-spaced day
        self.assertIsNone(next(j for j in jobs if j["id"] == "amz:search:10588001")["posted"])

    def test_a_long_list_is_read_page_by_page_until_a_short_page(self):
        full = json.loads(sample("amazon_jobs_search.json"))
        with mock.patch.object(scan, "AMAZON_PAGE", 2):
            def reply(url):
                offset = int(re.search(r"offset=(\d+)", url)[1])
                return json.dumps({"hits": 3, "jobs": full["jobs"][offset:offset + 2]}).encode()
            site = amazon_site(reply)
            jobs, errors = self.run_scan(site)
        self.assertEqual((len(jobs), errors), (3, {}))
        self.assertEqual([re.search(r"offset=(\d+)", u)[1] for u in self.pages(site)], ["0", "2"])
        self.assertNotIn("amazon", scan._cut_short)

    def test_more_pages_than_gus_reads_means_nothing_can_be_called_closed(self):
        full = json.loads(sample("amazon_jobs_search.json"))
        with mock.patch.object(scan, "AMAZON_PAGE", 1), mock.patch.object(scan, "AMAZON_PAGES", 2):
            site = amazon_site(lambda url: json.dumps({"hits": 3, "jobs": full["jobs"][:1]}).encode())
            jobs, _ = self.run_scan(site)
        self.assertEqual(len(self.pages(site)), 2)
        self.assertIn("amazon", scan._cut_short)

    def test_it_reads_once_a_day_however_often_scans_run(self):
        site = amazon_site()
        self.run_scan(site)
        jobs, errors = self.run_scan(site)
        self.assertEqual((jobs, errors, len(self.pages(site))), ([], {}, 1))

    def test_a_search_somewhere_else_is_refused_and_shown_on_the_health_page(self):
        self.search = "https://www.amazon.co.uk/search?q=product+manager"
        jobs, errors = self.run_scan(amazon_site())
        self.assertEqual(jobs, [])
        self.assertIn("isn't a search on amazon.jobs", errors["Amazon"])

    def test_a_page_that_is_not_job_results_is_reported_not_read_as_no_jobs(self):
        jobs, errors = self.run_scan(amazon_site(b"<html>Please verify you are a human</html>"))
        self.assertEqual(jobs, [])
        self.assertIn("no longer answers with job results", errors["Amazon"])
