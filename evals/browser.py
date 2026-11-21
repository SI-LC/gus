"""Requirement checks: each file in evals/cases runs in a real browser against a fresh install. Any page or script error
fails the case too. See evals/README.md.

    python3 -m evals.browser          # needs evals/requirements.txt and `playwright install chromium`
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from playwright.sync_api import Error, sync_playwright

from evals import demo
from evals.world import World

CASES = Path(__file__).parent / "cases"
WAIT_MS = 5000
RUN_MS = 90000  # a whole scan of the made-up boards, start to finish
PLACES = {"status bar": "#ticker", "progress": "#progress", "message": "#toast.on"}
GIVENS = ("empty", "signed out", "scanned", "scanned, signed out", "scanned after a job site", "scanned with a role only a job site has", "scanned with a board that says no", "demo")


def step(page, text):
    if m := re.fullmatch(r"open (.+)", text):
        page.locator("nav a", has_text=m[1]).click()
        page.locator("#main h1").first.wait_for(timeout=WAIT_MS)
    elif m := re.fullmatch(r'see "(.+)"', text):
        page.locator("#main").get_by_text(m[1]).first.wait_for(timeout=WAIT_MS)
    elif m := re.fullmatch(r'not see "(.+)"', text):  # gives the page the same time to take something away as `see` gives it to show something
        page.locator("#main").get_by_text(m[1]).first.wait_for(state="detached", timeout=WAIT_MS)
        assert m[1] not in page.locator("#main").inner_text(), f'"{m[1]}" is on the page'
    elif m := re.fullmatch(r'banner says "(.+)"', text):
        page.locator("#banner").get_by_text(m[1]).first.wait_for(timeout=WAIT_MS)
    elif text == "no banner":
        page.locator("#banner[hidden]").wait_for(state="attached", timeout=WAIT_MS)
    elif m := re.fullmatch(r'menu says "(.+)"', text):
        page.locator("#nav").get_by_text(m[1]).first.wait_for(timeout=WAIT_MS)
    elif m := re.fullmatch(r'status bar says "(.+)"', text):
        page.locator("#ticker").get_by_text(m[1]).first.wait_for(timeout=WAIT_MS)
    elif m := re.fullmatch(r'status bar doesn\'t say "(.+)"', text):
        assert m[1] not in page.locator("#ticker").inner_text().lower(), f'"{m[1]}" is in the status bar'
    elif m := re.fullmatch(r'"(.+)" appears before "(.+)"', text):
        shown = page.locator("#main").inner_text()
        assert m[1] in shown and m[2] in shown and shown.index(m[1]) < shown.index(m[2]), f"{m[1]!r} isn't before {m[2]!r}"
    elif m := re.fullmatch(r'progress says "(.+)"', text):  # the strip under the status bar while a scan runs, and after
        page.locator("#progress:not([hidden])").get_by_text(m[1]).first.wait_for(timeout=RUN_MS)
    elif text == "no progress":
        page.locator("#progress[hidden]").wait_for(state="attached", timeout=WAIT_MS)
    elif m := re.fullmatch(r'click "(.+)" in the (status bar|progress|message)', text):
        page.locator(PLACES[m[2]]).get_by_text(m[1], exact=True).first.click()
    elif m := re.fullmatch(r'click "(.+)"', text):
        page.locator("#main").get_by_text(m[1], exact=True).first.click()
    elif m := re.fullmatch(r'pick "(.+)"', text):
        page.locator("#main .chips-row a", has_text=m[1]).first.click()
    elif m := re.fullmatch(r'"(.+)" is picked', text):
        page.locator("#main .chips-row a.on", has_text=m[1]).first.wait_for(timeout=WAIT_MS)
    elif m := re.fullmatch(r'drag "(.+)" to "(.+)"', text):  # a role, on to the stage with that heading: a board column or a group in the list
        stage = page.locator("#main [data-drop]").filter(has=page.locator("h3", has_text=m[2])).first
        page.locator("#main [data-drag]", has_text=m[1]).first.drag_to(stage, timeout=WAIT_MS)
    elif m := re.fullmatch(r'type "(.+)" into "(.+)"', text):
        page.get_by_label(m[2], exact=True).fill(m[1])
    elif text == "the page is refreshing slowly":  # a background refresh is under way and its answer arrives late, as on a busy Pi
        page.evaluate("""() => { const f = window.fetch; window.fetch = async (u, o) => { const r = await f(u, o);
          if (String(u).includes("/api/state")) await new Promise(ok => setTimeout(ok, 1500)); return r; }; window.refreshed = load(); }""")
    elif text == "the refresh arrives":
        page.evaluate("() => window.refreshed")
    elif text == "Gus stops answering":  # restarting, or the connection dropped: nothing sent from the page arrives
        page.route("**/api/**", lambda r: r.abort() if r.request.method == "POST" else r.fallback())
    elif m := re.fullmatch(r'message says "(.+)"', text):
        page.locator("#toast.on", has_text=m[1]).wait_for(timeout=WAIT_MS)
    elif m := re.fullmatch(r'Claude moves "(.+)" to (\w+), saying "(.+)"', text):  # over MCP, as Claude in a chat would
        claude_changes(page, m[1], m[3], stage=m[2])
    elif m := re.fullmatch(r'Claude renames "(.+)" to "(.+)", saying "(.+)"', text):
        claude_changes(page, m[1], m[3], title=m[2])
    elif m := re.fullmatch(r'Claude points "(.+)" at "(.+)", saying "(.+)"', text):
        claude_changes(page, m[1], m[3], url=m[2])
    elif text == "reload":
        page.reload()
        page.locator("#main h1").first.wait_for(timeout=WAIT_MS)
    else:
        raise ValueError(f"Unknown step: {text}")


def claude_changes(page, role, reason, **change):
    """Turn on "Let Claude make changes", find the role by its title and change it through /mcp, with a reason."""
    base = "/".join(page.url.split("/")[:3])
    page.request.post(f"{base}/api/settings", data={"changes": {"mcp_allow_writes": True}})

    def tool(name, arguments):
        reply = page.request.post(f"{base}/mcp", data={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                         "params": {"name": name, "arguments": arguments}}).json()
        out = json.loads(reply["result"]["content"][0]["text"])
        assert "error" not in out, f"{name}: {out['error']}"
        return out

    roles = [r for r in tool("list_roles", {"query": role, "stage": ["new", "shortlisted", "preparing", "applied",
                                                                    "interviewing", "closed", "skipped"]})["roles"]
             if r.get("title") == role]
    assert roles, f'Claude can\'t find "{role}"'
    tool("update_role", {"role_id": roles[0]["role_id"], **change, "reason": reason})


def scan_once(givens) -> dict:
    """One scanned install for each kind the cases start from. Each case gets its own copy, as a scan of the made-up
    boards always comes out the same. The scans run at the same time, each in its own folder: a scan is mostly Gus
    pausing between requests, so four take as long as one."""
    def scan(given):
        world = World(signed_out="signed out" in given, job_site="after a job site" in given, refusing="says no" in given,
                      job_site_only="only a job site" in given)
        world.scan()
        return world

    kinds = sorted({g for g in givens if g.startswith("scanned")})
    with ThreadPoolExecutor(max_workers=len(kinds) or 1) as pool:
        return dict(zip(kinds, pool.map(scan, kinds)))


def fresh_world(given, scanned) -> World:
    """An install for the case to start from."""
    if given == "demo":
        return demo.build()
    if not given.startswith("scanned"):
        return World(new=True, signed_out="signed out" in given)
    return scanned[given].copy()


def run_case(browser, case, scanned) -> str:
    """The reason it failed, or an empty string if it passed."""
    world = fresh_world(case["given"], scanned)
    try:
        world.start()
        page = browser.new_page()
        problems = []
        page.on("pageerror", lambda e: problems.append(f"Script error: {e}"))
        page.on("console", lambda m: problems.append(f"Console error: {m.text}") if m.type == "error" and "net::" not in m.text else None)
        page.route("**/*", lambda r: r.continue_() if r.request.url.startswith(world.url) else r.abort())
        page.goto(world.url)
        for text in case["check"]:
            try:
                step(page, text)
            except (AssertionError, Error) as e:
                return f"{text}: {str(e).splitlines()[0]}\n      The page says: {page.locator('body').inner_text()[:300]!r}"
            if problems:
                return f"{text}: {problems[0]}"
        page.close()
        return ""
    finally:
        world.close()


def main():
    cases = [yaml.safe_load(p.read_text()) for p in sorted(CASES.glob("*.yaml"))]
    for c in cases:
        if not (c.get("id") and c.get("requirement") and c.get("given") in GIVENS and c.get("check")):
            sys.exit(f"Case {c.get('id')} needs an id, a requirement, given ({' or '.join(GIVENS)}) and a check")
    failed, scanned = 0, scan_once(c["given"] for c in cases)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for case in cases:
            reason = run_case(browser, case, scanned)
            print(f"  {'✗' if reason else '✓'} {case['id']}: {case['requirement']}")
            if reason:
                failed += 1
                print(f"      {reason}")
        browser.close()
    for world in scanned.values():
        world.close()
    print(f"{len(cases) - failed} of {len(cases)} requirements passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
