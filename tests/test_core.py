"""python3 -m unittest discover -s tests"""
import unittest
from datetime import datetime

import email
import re

from radar import agent, ai, calendar_invites as cal, inbox
from radar.network import distance, paths_for
from radar.service import due

CFG = {"scan_times": "08:00, 13:00, 18:00", "agent_times_weekdays": "08:00", "agent_times_weekends": "",
       "watchlist_every_days": 3, "watchlist_time": "07:30", "moves_day": "Mon", "moves_time": "08:30"}
DONE = {"watchlist": "2026-11-04", "moves": "2026-11-02"}


def at(day, h, m=0):
    return datetime(2026, 11, day, h, m)


class Schedule(unittest.TestCase):
    def test_nothing_before_first_slot(self):
        self.assertEqual(due(at(4, 7), CFG, DONE), [])

    def test_morning_slot_has_scan_and_discovery(self):
        self.assertEqual(due(at(4, 8), CFG, DONE), ["scan", "agent"])

    def test_each_slot_runs_once(self):
        s = {**DONE, "scan": "2026-11-04 08:00", "agent": "2026-11-04 08:00"}
        self.assertEqual(due(at(4, 8, 30), CFG, s), [])
        self.assertEqual(due(at(4, 13), CFG, s), ["scan"])

    def test_no_discovery_at_weekends(self):
        self.assertEqual(due(at(7, 8), CFG, {"watchlist": "2026-11-07", "moves": "2026-11-02"}), ["scan"])

    def test_moves_on_monday_after_morning_run(self):
        s = {"watchlist": "2026-11-09", "moves": "2026-11-02", "scan": "2026-11-09 08:00", "agent": "2026-11-09 08:00"}
        self.assertEqual(due(at(9, 8), CFG, s), [])
        self.assertEqual(due(at(9, 8, 30), CFG, s), ["moves"])


class Budget(unittest.TestCase):
    def test_zero_blocks_every_run(self):
        with self.assertRaises(ai.BudgetReached):
            ai._check_budget({"ai_daily_budget_usd": 0})
        with self.assertRaises(ai.BudgetReached):
            ai._check_budget({})

    def test_room_left_allows_a_run(self):
        ai._check_budget({"ai_daily_budget_usd": 1000000})


class Inbox(unittest.TestCase):
    SENDERS = ["linkedin.com", "ashbyhq.com"]
    SUBJECTS = re.compile("interview|next steps", re.I)

    def test_only_matching_emails_are_opened(self):
        self.assertTrue(inbox.matches("LinkedIn <messages-noreply@linkedin.com>", "New message", self.SENDERS, self.SUBJECTS))
        self.assertTrue(inbox.matches("Sam <sam@acme.com>", "Interview on Friday", self.SENDERS, self.SUBJECTS))
        self.assertFalse(inbox.matches("Mum <mum@example.com>", "Sunday lunch", self.SENDERS, self.SUBJECTS))

    def test_sender_name_alone_does_not_match(self):
        self.assertFalse(inbox.matches('"linkedin.com" <spam@example.com>', "Hello", self.SENDERS, None))

    def test_body_prefers_plain_text(self):
        msg = email.message_from_string("Content-Type: text/plain; charset=utf-8\n\nHello   there\nworld")
        self.assertEqual(inbox.body_text(msg), "Hello there world")

    def test_html_body_is_stripped(self):
        msg = email.message_from_string("Content-Type: text/html; charset=utf-8\n\n<p>Role <b>open</b></p>")
        self.assertEqual(inbox.body_text(msg), "Role open")


ICS = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VEVENT
UID:abc@google.com
DTSTART:20261110T100000Z
DTEND:20261110T103000Z
SUMMARY:Intro chat\\, Acme and
  Pat
ORGANIZER;CN=Sam Lee:mailto:sam@acme.io
ATTENDEE;CN=Pat Doe:mailto:pat@example.com
LOCATION:https://meet.google.com/abc
END:VEVENT
END:VCALENDAR
"""


class Calendar(unittest.TestCase):
    def test_parses_a_google_invite(self):
        ev = cal.parse_ics(ICS)[0]
        self.assertEqual(ev["title"], "Intro chat, Acme and Pat")
        self.assertEqual(ev["start"], "2026-11-10T10:00:00+00:00")
        self.assertEqual(ev["organizer"], {"name": "Sam Lee", "email": "sam@acme.io"})

    def test_cancelled(self):
        self.assertTrue(cal.parse_ics(ICS.replace("METHOD:REQUEST", "METHOD:CANCEL"))[0]["cancelled"])

    def test_personal_meetings_are_dropped(self):
        ev = {"title": "Dentist", "organizer": {"name": "Smile Clinic", "email": "hi@smile.co.uk"}, "attendees": []}
        self.assertIsNone(cal.relevance(ev, {"Acme": {"status": "watched"}}, {}))

    def test_tracked_account_is_kept(self):
        ev = {"title": "Catch up", "organizer": {"name": "Sam", "email": "sam@acme.io"}, "attendees": []}
        self.assertEqual(cal.relevance(ev, {"Acme": {"status": "watched"}}, {})["account"], "Acme")

    def test_mapped_person_is_kept(self):
        ev = {"title": "Catch up", "organizer": {"name": "Sam Lee", "email": "sam@gmail.com"}, "attendees": []}
        self.assertEqual(cal.relevance(ev, {}, {"p": {"name": "Sam Lee", "company": "Acme"}})["person_id"], "p")

    def test_interview_is_kept(self):
        ev = {"title": "Interview with the team", "attendees": []}
        self.assertIsNotNone(cal.relevance(ev, {}, {}))


class AgentLimits(unittest.TestCase):
    def test_email_agent_has_no_internet(self):
        self.assertFalse({"WebSearch", "WebFetch"} & set(agent.OFFLINE))

    def test_web_agents_cant_read_email(self):
        self.assertIn("Read(./data/emails.json)", agent.NO_EMAIL)


class Distance(unittest.TestCase):
    CONNS = [{"name": "Ada Example", "url": "u", "company": "Acme Ltd", "position": "PM"}]

    def label(self, person, conns=()):
        return distance(paths_for(person, list(conns), {}, ["Globex"]))

    def test_estimates_are_marked(self):
        d = self.label({"name": "Pat", "company": "Acme"}, self.CONNS)
        self.assertEqual((d["label"], d["certain"]), ("~2nd", False))

    def test_confirmed_beats_estimate(self):
        d = self.label({"name": "Pat", "company": "Acme", "degree": 3}, self.CONNS)
        self.assertEqual((d["label"], d["certain"]), ("3rd+", True))

    def test_reply_means_in_touch(self):
        self.assertEqual(self.label({"name": "Pat", "company": "Acme", "stage": "replied"})["label"], "In touch")

    def test_no_route_is_cold(self):
        self.assertEqual(self.label({"name": "Pat", "company": "Other"})["label"], "3rd+")


if __name__ == "__main__":
    unittest.main()
