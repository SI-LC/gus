"""Research runs spend their steps on research: Gus's own code refreshes signals, hands the agent what's waiting
and takes each kind of change as one checked list. What research reads, finds and scores is unchanged."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar import agent, ai, companies as C, events as E, signals as S

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class Files(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.paths = {"SIGNALS": self.d / "signals.json", "EVENTS": self.d / "events.json", "COMPANIES": self.d / "companies.json"}
        for module in (S, E, C):
            for name, path in self.paths.items():
                if hasattr(module, name):
                    p = mock.patch.object(module, name, path)
                    p.start()
                    self.addCleanup(p.stop)

    def write(self, name, data):
        self.paths[name].write_text(json.dumps(data))

    def stored(self, name):
        return json.loads(self.paths[name].read_text())


class WhatsWaiting(Files):
    def test_only_new_signals_in_full_without_the_housekeeping(self):
        self.write("SIGNALS", {"a": {"source": "Feed", "kind": "signals", "title": "Acme raises", "text": "x" * 5000, "url": "https://a", "status": "new",
                                     "first_seen": "2026-11-01", "mention_checked": True},
                               "b": {"source": "Feed", "url": "https://b", "status": "processed"}})
        out = S.waiting()
        self.assertEqual([s["id"] for s in out], ["a"])
        self.assertNotIn("mention_checked", out[0])
        self.assertEqual(len(out[0]["text"]), 5000, "nothing is shortened: research reads what it always read")


class MarkSignals(Files):
    def test_a_list_is_marked_in_one_go(self):
        self.write("SIGNALS", {"a": {"source": "f", "url": "https://a", "status": "new"}, "b": {"source": "f", "url": "https://b", "status": "new"}})
        out = S.mark([{"id": "a", "status": "processed", "company": "Acme", "event_id": "acme:funding:2026-11-01"}, {"id": "b", "status": "ignored"}])
        self.assertEqual(out, {"a": "processed", "b": "ignored"})
        self.assertEqual(self.stored("SIGNALS")["a"]["company"], "Acme")
        self.assertEqual(self.stored("SIGNALS")["b"]["status"], "ignored")

    def test_a_wrong_id_status_or_field_is_turned_down_and_the_rest_still_saved(self):
        self.write("SIGNALS", {"a": {"source": "f", "url": "https://a", "status": "new"}})
        out = S.mark([{"id": "nope", "status": "ignored"}, {"id": "a", "status": "done"}, {"id": "a", "status": "ignored", "url": "https://evil"}])
        self.assertIn("no such signal", out["nope"])
        self.assertEqual(self.stored("SIGNALS")["a"]["url"], "https://a", "a signal's own details can't be rewritten")
        self.assertIn("not saved", out["a"])


class AddEvents(Files):
    def setUp(self):
        super().setUp()
        self.write("COMPANIES", {"Acme": {"status": "watched"}})
        self.write("SIGNALS", {"s1": {"source": "f", "url": "https://n/1", "status": "new"}})
        self.event = {"account": "acme ltd", "type": "funding", "summary": "Raised a round", "url": "https://n/1", "date": "2026-11-01", "source": "signal:s1"}

    def test_events_are_added_as_a_list_and_the_signal_points_at_its_event(self):
        out = E.add_many([self.event, dict(self.event, url="https://n/2", type="launch", source="agent")])
        self.assertEqual(out, ["acme:funding:2026-11-01", "acme:launch:2026-11-01"])
        self.assertEqual(self.stored("EVENTS")[out[0]]["account"], "Acme", "the account is spelled as Gus has it")
        self.assertEqual(self.stored("SIGNALS")["s1"]["event_id"], out[0])

    def test_the_same_link_twice_is_one_event(self):
        self.assertEqual(E.add_many([self.event]), E.add_many([self.event]))
        self.assertEqual(len(self.stored("EVENTS")), 1)

    def test_an_unknown_company_or_a_bad_event_is_turned_down_with_the_reason(self):
        out = E.add_many([dict(self.event, account="Nobody"), dict(self.event, date="soon"), dict(self.event, source="web")])
        self.assertIn("isn't in companies.json", out[0])
        self.assertIn("real date", out[1])
        self.assertIn("source", out[2])
        self.assertFalse(self.paths["EVENTS"].exists() and self.stored("EVENTS"))


class SetCompanies(Files):
    def test_several_companies_are_updated_in_one_go(self):
        self.write("COMPANIES", {"Acme": {"status": "candidate", "fit": 6}})
        out = C.set_many({"Acme": {"status": "watched", "fit": 8, "fit_why": "Sells to developers"}, "Newco": {"source": "TechCrunch", "careers_url": "https://newco.example/careers"}})
        self.assertEqual(out, {"Acme": "saved", "Newco": "added"})
        c = self.stored("COMPANIES")
        self.assertEqual((c["Acme"]["fit"], c["Acme"]["previous_fit"], c["Acme"]["status"]), (8, 6, "watched"))
        self.assertEqual(c["Newco"]["status"], "candidate", "a new company starts as a candidate")

    def test_a_company_is_found_however_it_is_spelled(self):
        self.write("COMPANIES", {"Acme": {"status": "watched"}})
        self.assertEqual(C.set_many({"ACME Ltd": {"headcount": "200"}}), {"ACME Ltd": "saved"})
        self.assertEqual(list(self.stored("COMPANIES")), ["Acme"])

    def test_the_candidates_own_choices_cant_be_changed(self):
        self.write("COMPANIES", {"Gone": {"status": "rejected", "vetoed": True}, "Loved": {"status": "watched", "favourite": True, "tier": "A", "tier_locked": True}})
        out = C.set_many({"Gone": {"status": "candidate"}, "Loved": {"status": "rejected", "tier": "C", "fit": 3, "favourite": False}})
        c = self.stored("COMPANIES")
        self.assertIn("removed", out["Gone"])
        self.assertEqual(c["Gone"]["status"], "rejected")
        self.assertEqual((c["Loved"]["status"], c["Loved"]["tier"], c["Loved"]["favourite"], c["Loved"]["fit"]), ("watched", "A", True, 3))
        self.assertIn("kept", out["Loved"])

    def test_a_value_the_schema_turns_down_is_not_saved(self):
        self.write("COMPANIES", {"Acme": {"status": "watched"}})
        self.assertIn("status", C.set_many({"Acme": {"status": "great"}})["Acme"])
        self.assertEqual(self.stored("COMPANIES")["Acme"], {"status": "watched"})

    def test_known_says_what_gus_has_for_each_name(self):
        self.write("COMPANIES", {"Acme": {"status": "watched", "tier": "A"}, "Gone": {"status": "rejected", "vetoed": True}})
        self.assertEqual(C.known(["acme inc", "Gone", "Newco"]), {"acme inc": "Acme: watched, tier A", "Gone": "Gone: removed by the candidate, never re-add", "Newco": None})


class TheRun(unittest.TestCase):
    def run_agent(self, playbook):
        d = Path(tempfile.mkdtemp())
        seen = {}

        def fake_ai(prompt, **k):
            seen.update(prompt=prompt, brief=json.loads((d / f"brief-{playbook}.json").read_text()), **k)
            return "report"
        with mock.patch.object(agent, "DATA", d), mock.patch.object(agent, "refresh", lambda: seen.setdefault("order", ["refresh"])), \
                mock.patch.object(agent, "brief", lambda p: {"playbook": p}), mock.patch.object(agent.ai, "run", fake_ai), \
                mock.patch.object(agent.ai, "check_budget", lambda cfg: None), mock.patch.object(agent, "vcs_path", lambda: None):
            agent.run(playbook)
        return seen

    def test_signals_are_refreshed_by_gus_before_the_agent_starts(self):
        seen = self.run_agent("discover")
        self.assertEqual(seen["order"], ["refresh"])
        for command in ("radar.feeds", "radar.social", "radar.x", "radar.inbox"):
            self.assertNotIn(command, seen["prompt"], "refreshing is Gus's job, not a step of the conversation")

    def test_the_agent_is_handed_what_is_waiting(self):
        seen = self.run_agent("watchlist")
        self.assertEqual(seen["brief"], {"playbook": "watchlist"})
        self.assertIn("data/brief-watchlist.json", seen["prompt"])

    def test_the_agent_starts_with_the_profile_the_playbook_and_the_brief_in_hand(self):
        """Reading them took a run its first steps, five of them when the brief was long."""
        seen = self.run_agent("watchlist")
        self.assertIn((PROMPTS / "watchlist.md").read_text(), seen["prompt"])
        self.assertIn('"playbook": "watchlist"', seen["prompt"])

    def test_a_web_run_is_kept_out_of_the_big_data_files(self):
        from radar import guard
        self.assertEqual(self.run_agent("discover")["settings"], agent.GUARD)
        bash = lambda c: guard.refusal({"tool_name": "Bash", "tool_input": {"command": c}})
        for command in ('grep -n "Acme" data/signals.json', "tail -n +11800 data/signals.json | grep company", "cd data && cat companies.json",
                        "python3 -m radar.companies known Acme; cat data/companies.json"):
            self.assertIn("closed to this run", bash(command) or "", command)
        self.assertIn("radar.companies", guard.refusal({"tool_name": "Read", "tool_input": {"file_path": "/home/me/gus/data/companies.json"}}))
        self.assertIn("brief", guard.refusal({"tool_name": "Grep", "tool_input": {"pattern": "x", "path": "data/signals.json"}}))
        for command in ("python3 -m radar.companies show Acme", """python3 -m radar.signals mark '[{"id": "feed:x", "status": "ignored"}]'""", "grep -n Acme data/events.json"):
            self.assertIsNone(bash(command), command)
        self.assertIsNone(guard.refusal({"tool_name": "Read", "tool_input": {"file_path": "data/brief-watchlist.json"}}))

    def test_a_flagged_companys_news_is_in_the_brief(self):
        d = Path(tempfile.mkdtemp())
        (d / "signals.json").write_text(json.dumps({
            "a": {"company": "Acme", "title": "Acme raises", "text": "x" * 900, "url": "https://example.com/a", "first_seen": "2026-01-02"},
            "b": {"company": "Acme", "title": "Older", "first_seen": "2026-01-01"}, "c": {"company": "Other", "title": "Not asked for"},
            **{f"n{n}": {"company": "Busy", "title": f"Item {n}", "first_seen": f"2026-01-{n + 10}"} for n in range(9)}}))
        with mock.patch.object(agent, "SIGNALS", d / "signals.json"):
            out = agent.news(["Acme", "Busy", "Quiet"])
        self.assertEqual([s["title"] for s in out["Acme"]], ["Acme raises", "Older"])
        self.assertEqual((len(out["Acme"][0]["text"]), len(out["Busy"]), out["Busy"][0]["title"]), (300, agent.NEWS_EACH, "Item 8"))
        self.assertNotIn("Quiet", out)

    def test_an_account_searched_for_people_this_week_waits(self):
        from datetime import date, timedelta
        d = Path(tempfile.mkdtemp())
        day = lambda n: str(date.today() - timedelta(days=n))
        (d / "c.json").write_text(json.dumps({"Fresh": {"status": "watched", "tier": "A", "people_checked": day(2)},
                                              "Due": {"status": "watched", "tier": "A", "people_checked": day(9)}, "Never": {"status": "watched", "tier": "A"}}))
        (d / "e.json").write_text("{}")
        with mock.patch.object(agent, "COMPANIES", d / "c.json"), mock.patch.object(agent, "PEOPLE", d / "e.json"), mock.patch.object(agent, "EVENTS", d / "e.json"):
            self.assertEqual(sorted(agent._short_of_people("A", with_news=False)), ["Due", "Never"])

    def test_several_boards_are_added_in_one_call(self):
        from radar import resolve
        d = Path(tempfile.mkdtemp())
        (d / "w.csv").write_text("company,ats,slug,note\n")
        asked = {}
        with mock.patch.object(resolve.sys, "argv", ["resolve", "--add", "Acme=https://acme.example/careers", "Plain Co"]), \
                mock.patch.object(resolve, "watchlist_path", lambda: d / "w.csv"), mock.patch("radar.companies.sync", lambda: None), \
                mock.patch.object(resolve, "resolve", lambda name, careers=None: asked.setdefault(name, careers) and None or ("ashby", name.lower(), 3)), \
                mock.patch("builtins.print"):
            resolve.main()
        self.assertEqual(asked, {"Acme": "https://acme.example/careers", "Plain Co": None})
        self.assertIn("Acme,ashby,acme", (d / "w.csv").read_text())

    def test_people_are_mapped_at_several_companies_in_one_call(self):
        from radar import people
        d = Path(tempfile.mkdtemp())
        (d / "c.json").write_text(json.dumps({"Arva AI": {}, "Acme": {}}))
        with mock.patch.object(people, "COMPANIES", d / "c.json"):
            self.assertEqual(people.asked_for(["Acme", "Arva AI"]), ["Acme", "Arva AI"])
            self.assertEqual(people.asked_for(["Arva", "AI"]), ["Arva AI"], "one name typed without quotes is still one company")

    def test_nothing_is_refreshed_when_the_limit_is_already_reached(self):
        calls = []
        with mock.patch.object(agent, "refresh", lambda: calls.append("refresh")), mock.patch.object(agent, "vcs_path", lambda: None), \
                mock.patch.object(agent.ai, "check_budget", mock.Mock(side_effect=ai.BudgetReached("limit"))):
            with self.assertRaises(ai.BudgetReached):
                agent.run("discover")
        self.assertEqual(calls, [])

    def test_the_agent_can_run_the_list_commands(self):
        for command in ("signals", "events", "companies"):
            self.assertIn(f"Bash(python3 -m radar.{command}:*)", agent.TOOLS)

    def test_the_playbooks_use_the_list_commands_and_still_score(self):
        discover, watchlist = (PROMPTS / "discover.md").read_text(), (PROMPTS / "watchlist.md").read_text()
        for command in ("python3 -m radar.signals mark", "python3 -m radar.events add-many", "python3 -m radar.companies set", "python3 -m radar.companies known"):
            self.assertIn(command, discover)
        self.assertIn("python3 -m radar.companies set", watchlist)
        self.assertIn("python3 -m radar.companies show", watchlist)
        self.assertIn("score it exactly as `prompts/triage.md`", discover, "the agent still scores what it finds, with the company's research in view")
        self.assertIn("Score\n   with prompts/triage.md", agent.PROMPT)


class WhatARunUsed(unittest.TestCase):
    def test_web_searches_and_steps_are_recorded(self):
        """Claude Code leaves `usage.server_tool_use` at 0 and counts searches per model."""
        reply = {"result": "ok", "total_cost_usd": 1, "num_turns": 42, "usage": {"server_tool_use": {"web_search_requests": 0}},
                 "modelUsage": {"claude-opus-9": {"costUSD": 0.9, "webSearchRequests": 12}, "claude-haiku-9": {"costUSD": 0.1, "webSearchRequests": 3}}}
        rows = []
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=json.dumps(reply), stderr="", returncode=0)), \
                mock.patch.object(ai, "load_config", lambda: {"ai_provider": "anthropic_api"}), mock.patch.object(ai, "secret", lambda k: "k"), \
                mock.patch.object(ai.usage, "record", lambda **k: rows.append(k)):
            ai.run("hello", task="agent", budget=False)
        self.assertEqual((rows[0]["searches"], rows[0]["turns"]), (15, 42))

    def test_tokens_count_the_helpers_too(self):
        """`usage` covers the main agent only. `modelUsage` has every model the run used, the helpers that read pages for it included."""
        reply = {"result": "ok", "total_cost_usd": 1, "num_turns": 3, "permission_denials": [{}, {}],
                 "usage": {"input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10, "output_tokens": 5},
                 "modelUsage": {"claude-opus-9": {"inputTokens": 2, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 10, "outputTokens": 5},
                                "claude-haiku-9": {"inputTokens": 1, "cacheReadInputTokens": 40, "cacheCreationInputTokens": 30, "outputTokens": 7}}}
        rows = []
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=json.dumps(reply), stderr="", returncode=0)), \
                mock.patch.object(ai, "load_config", lambda: {"ai_provider": "anthropic_api"}), mock.patch.object(ai, "secret", lambda k: "k"), \
                mock.patch.object(ai.usage, "record", lambda **k: rows.append(k)):
            ai.run("hello", task="agent", budget=False)
        self.assertEqual((rows[0]["input_tokens"], rows[0]["output_tokens"]), (183, 12))
        self.assertEqual((rows[0]["cache_read_tokens"], rows[0]["cache_write_tokens"], rows[0]["refused"]), (140, 40, 2))

    def test_tokens_fall_back_to_the_runs_own_count(self):
        reply = {"result": "ok", "usage": {"input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10, "output_tokens": 5}}
        rows = []
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout=json.dumps(reply), stderr="", returncode=0)), \
                mock.patch.object(ai, "load_config", lambda: {"ai_provider": "anthropic_api"}), mock.patch.object(ai, "secret", lambda k: "k"), \
                mock.patch.object(ai.usage, "record", lambda **k: rows.append(k)):
            ai.run("hello", task="agent", budget=False)
        self.assertEqual((rows[0]["input_tokens"], rows[0]["output_tokens"], rows[0]["refused"]), (112, 5, 0))


if __name__ == "__main__":
    unittest.main()
