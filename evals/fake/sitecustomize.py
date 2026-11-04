"""Loaded by every Python process in a smoke test: one made-up job board, and no way to reach the real internet."""
import io
import json
import socket
import urllib.error
import urllib.request

BOARD = "https://boards-api.greenhouse.io/v1/boards/harborlight/jobs"
ROLES = [(1, "Senior Product Manager", "London"), (2, "Product Manager, Growth", "London"), (3, "Product Owner", "London"),
         (4, "Software Engineer", "London"), (5, "Product Manager", "San Francisco")]


def listing(n, title, place):
    return {"id": n, "title": title, "absolute_url": f"https://example.com/roles/{n}", "location": {"name": place},
            "first_published": "2026-01-01T09:00:00Z"}


def reply_for(url):
    if url == BOARD:
        return {"jobs": [listing(*r) for r in ROLES]}
    for r in ROLES:
        if url == f"{BOARD}/{r[0]}":
            return {**listing(*r), "content": f"<p>Own the {r[1]} roadmap at Harborlight.</p>", "offices": []}
    return None


class Response(io.BytesIO):
    headers = {}


def urlopen(request, *args, **kwargs):
    url = request if isinstance(request, str) else request.full_url
    body = reply_for(url)
    if body is None:  # a 404 fails straight away, where a dropped connection would be retried
        raise urllib.error.HTTPError(url, 404, "Not in the smoke test's world", {}, None)
    return Response(json.dumps(body).encode())


real_connect = socket.socket.connect


def connect(self, address):
    if self.family == socket.AF_INET and address[0] != "127.0.0.1":
        raise OSError(f"Smoke tests can't reach {address[0]}")
    if self.family == socket.AF_INET6:
        raise OSError("Smoke tests can't reach the internet")
    return real_connect(self, address)


urllib.request.urlopen = urlopen
socket.socket.connect = connect
