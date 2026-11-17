"""The tests never use the internet. A test that looks up any host but this computer fails here, naming the host, instead
of waiting 20 seconds for an answer that changes from day to day. The check is put in place when the tests are collected,
so it covers every test file in a full run (python3 -m unittest discover -s tests), and deploy/run_tests.py loads this
file first in each process it starts. It doesn't cover the separate processes some tests start."""
import socket
import unittest

from radar import web

LOCAL = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "", None}  # nosec B104: names this computer answers to, nothing is opened
real_lookup = socket.getaddrinfo


class UsedTheInternet(AssertionError):
    """Not an OSError, so code that carries on after a failed request doesn't hide it."""


def local_only(host, *args, **kwargs):
    name = host.decode() if isinstance(host, bytes) else host
    if name not in LOCAL:
        raise UsedTheInternet(f"A test looked up {name}. Tests must not use the internet: mock the lookup that reached it.")
    return real_lookup(host, *args, **kwargs)


socket.getaddrinfo = local_only

# Nor do they ask git which release this is, as every request Gus sends would. Python waits for git by sleeping in short
# steps, and a test that counts sleeps to check Gus's pacing then counts those too whenever git is slow to answer. The
# tests of the release itself (test_core.py) set this back for as long as they run.
web._version = (float("inf"), "0.0.0")


class Offline(unittest.TestCase):
    def test_a_lookup_of_another_computer_fails_the_test_and_names_the_host(self):
        with self.assertRaisesRegex(UsedTheInternet, "gus.example"):
            socket.create_connection(("gus.example", 443), timeout=1)

    def test_this_computer_can_still_be_reached_for_the_tests_that_start_a_cockpit(self):
        self.assertTrue(socket.getaddrinfo("localhost", 80))
        self.assertTrue(socket.getaddrinfo("127.0.0.1", 80))
