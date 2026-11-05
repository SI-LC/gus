"""Requirement checks: each file in evals/cases runs in a real browser against a fresh install. Any page or script error
fails the case too. See evals/README.md.

    python3 -m evals.browser          # needs evals/requirements.txt and `playwright install chromium`
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from playwright.sync_api import Error, sync_playwright

from evals.world import World

CASES = Path(__file__).parent / "cases"
WAIT_MS = 5000
GIVENS = ("empty", "scanned")


def step(page, text):
    if m := re.fullmatch(r"open (.+)", text):
        page.locator("nav a", has_text=m[1]).click()
        page.locator("#main h1").first.wait_for(timeout=WAIT_MS)
    elif m := re.fullmatch(r'see "(.+)"', text):
        page.locator("#main").get_by_text(m[1]).first.wait_for(timeout=WAIT_MS)
    elif m := re.fullmatch(r'not see "(.+)"', text):
        assert m[1] not in page.locator("#main").inner_text(), f'"{m[1]}" is on the page'
    elif m := re.fullmatch(r'"(.+)" appears before "(.+)"', text):
        shown = page.locator("#main").inner_text()
        assert m[1] in shown and m[2] in shown and shown.index(m[1]) < shown.index(m[2]), f"{m[1]!r} isn't before {m[2]!r}"
    elif m := re.fullmatch(r'click "(.+)"', text):
        page.locator("#main").get_by_text(m[1], exact=True).first.click()
    elif m := re.fullmatch(r'pick "(.+)"', text):
        page.locator("#main .chips-row a", has_text=m[1]).first.click()
    elif m := re.fullmatch(r'"(.+)" is picked', text):
        page.locator("#main .chips-row a.on", has_text=m[1]).first.wait_for(timeout=WAIT_MS)
    elif text == "reload":
        page.reload()
        page.locator("#main h1").first.wait_for(timeout=WAIT_MS)
    else:
        raise ValueError(f"Unknown step: {text}")


def run_case(browser, case) -> str:
    """The reason it failed, or an empty string if it passed."""
    world = World()
    try:
        if case["given"] == "scanned":
            world.scan()
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
    failed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for case in cases:
            reason = run_case(browser, case)
            print(f"  {'✗' if reason else '✓'} {case['id']}: {case['requirement']}")
            if reason:
                failed += 1
                print(f"      {reason}")
        browser.close()
    print(f"{len(cases) - failed} of {len(cases)} requirements passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
