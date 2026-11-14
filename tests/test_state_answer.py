"""/api/state is the cockpit's every load and refresh: it must arrive compressed, be built once while nothing has changed,
and not be sent again to a page that already holds it. None of that may change what's in it."""
import gzip
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from radar import network, server


class StateAnswer(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        (self.data / "jobs.json").write_text("{}")
        self.built = 0
        self.payload = {"jobs": [{"id": f"gh:acme:{i}", "title": "Product Manager", "why": "Made-up reason " * 20} for i in range(50)]}

        def state():
            self.built += 1
            return self.payload
        for patch in (mock.patch.object(server, "DATA", self.data), mock.patch.object(server, "state", state),
                      mock.patch.object(server, "secret", lambda key: ""), mock.patch.dict(server.BUILT, key=None, tag="")):
            patch.start()
            self.addCleanup(patch.stop)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.url = f"http://127.0.0.1:{httpd.server_address[1]}/api/state"

    def get(self, **headers):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, headers=headers)) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_it_is_compressed_for_a_browser_that_accepts_it_and_says_the_same_thing(self):
        status, body, headers = self.get(**{"Accept-Encoding": "gzip, deflate, br"})
        self.assertEqual((status, headers["Content-Encoding"]), (200, "gzip"))
        self.assertEqual(json.loads(gzip.decompress(body)), self.payload)
        self.assertLess(len(body), len(json.dumps(self.payload)) / 3)
        self.assertEqual(headers["Content-Length"], str(len(body)))
        status, plain, headers = self.get()  # a caller that didn't ask gets it as it always was
        self.assertIsNone(headers["Content-Encoding"])
        self.assertEqual(json.loads(plain), self.payload)

    def test_a_page_that_holds_this_answer_is_not_sent_it_again(self):
        _, _, headers = self.get()
        self.assertTrue(headers["ETag"])
        status, body, again = self.get(**{"If-None-Match": headers["ETag"], "Accept-Encoding": "gzip"})
        self.assertEqual((status, body, again["ETag"]), (304, b"", headers["ETag"]))
        self.assertEqual(self.get(**{"If-None-Match": '"some-older-answer"'})[0], 200)

    def test_it_is_built_once_while_nothing_has_changed(self):
        for _ in range(3):
            self.get()
        self.assertEqual(self.built, 1)

    def test_a_change_to_the_data_is_in_the_very_next_answer(self):
        _, _, first = self.get()
        self.payload = {"jobs": [{"id": "gh:acme:1", "stage": "shortlisted"}]}
        with server.locked(self.data / "jobs.json") as jobs:  # the way every writer saves
            jobs["gh:acme:1"] = {"stage": "shortlisted"}
        status, body, second = self.get(**{"If-None-Match": first["ETag"]})
        self.assertEqual((status, json.loads(body)), (200, self.payload))
        self.assertNotEqual(first["ETag"], second["ETag"])
        self.assertEqual(self.built, 2)

    def test_a_new_file_or_a_run_under_way_builds_it_again(self):
        self.get()
        (self.data / "login_expired").write_text("{}")
        self.get()
        self.assertEqual(self.built, 2)
        with mock.patch.dict(server.service.running, {"scan": "2026-11-01T09:00:00+00:00"}):  # its progress moves every second
            self.get(), self.get()
        self.assertEqual(self.built, 4)

    def test_an_old_answer_is_not_trusted_for_ever(self):
        self.get()
        server.BUILT["at"] -= server.BUILT_FOR + 1
        self.get()
        self.assertEqual(self.built, 2)

    def test_other_pages_are_compressed_too_and_small_answers_are_left_alone(self):
        page = self.url.replace("/api/state", "/")
        with urllib.request.urlopen(urllib.request.Request(page, headers={"Accept-Encoding": "gzip"})) as r:
            self.assertEqual(r.headers["Content-Encoding"], "gzip")
            self.assertEqual(gzip.decompress(r.read()), server.PAGE.read_bytes())
        with urllib.request.urlopen(urllib.request.Request(page.rstrip("/") + "/icon-32.png", headers={"Accept-Encoding": "gzip"})) as r:
            self.assertIsNone(r.headers["Content-Encoding"])


class Distances(unittest.TestCase):
    """Each person is only compared with the connections that could matter. The routes found must be the ones a full comparison finds."""

    def test_the_routes_are_the_same_as_comparing_everyone_with_everyone(self):
        people = {"sam-reed@acme": {"name": "Sam Reed", "company": "Acme Ltd", "past_companies": ["Birchfield", "Cobaltworks AI"]},
                  "kit-lane@dunmore": {"name": "Kit Lane", "company": "Dunmore", "stage": "replied"},
                  "jo-tate@nowhere": {"name": "Jo Tate", "company": "Nowhere"}}
        conns = [{"name": "Alex Gray", "company": "Birchfield Inc", "position": "PM", "connected_on": "01 Jan 2020", "url": "https://x/1"},
                 {"name": "Sam Reed", "company": "Elsewhere", "position": "PM", "connected_on": "02 Jan 2020", "url": "https://x/2"},
                 {"name": "Noor Sage", "company": "acme", "position": "Designer", "connected_on": "03 Jan 2020", "url": "https://x/3"},
                 {"name": "Perry Oak", "company": "Emberlight Capital", "position": "Partner", "connected_on": "04 Jan 2020", "url": "https://x/4"},
                 {"name": "Quinn Ash", "company": "Cobaltworks", "position": "PM", "connected_on": "05 Jan 2020", "url": "https://x/5"},
                 {"name": "Reese Elm", "company": "", "position": "", "connected_on": "", "url": ""}]
        companies = {"Acme Ltd": {"investors": ["Emberlight Capital"]}, "Dunmore": {}}
        files = {network.NETWORK: {"connections": conns}, network.COMPANIES: companies}
        with mock.patch.object(network, "read", lambda path, default=None: files.get(path, {})), \
                mock.patch("radar.settings.load", lambda: {"my_employers": "Cobaltworks\n"}):
            found = network.distances(people)
        for pid, p in people.items():
            full = network.distance(network.paths_for(p, conns, companies.get(p["company"], {}), ["Cobaltworks"]))
            self.assertEqual(found[pid], full, pid)
        self.assertEqual([r["kind"] for r in found["sam-reed@acme"]["paths"]], ["direct", "same_company", "alumni_bridge", "alumni_bridge", "shared_employer"])
        self.assertEqual(found["jo-tate@nowhere"]["paths"], [])


if __name__ == "__main__":
    unittest.main()
