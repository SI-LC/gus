# Requirement checks

Each file in `cases/` is one thing the product must do, in plain words. Every pull request runs them in a real browser
against a fresh install with made-up data, and a failure blocks the merge. Any script error on the page fails the case too.

```yaml
id: shortlist-a-role
requirement: When I shortlist a role, it moves to my pipeline and stays there.
given: scanned
check:
  - open Role Inbox
  - click "Shortlist"
  - open Pipeline
  - see "Senior Product Manager"
  - reload
  - see "Senior Product Manager"
judge: Is it clear the role has moved, and where to find it?
```

- `given`: `empty` is a brand new install, so it starts on first run. `signed out` is the same with AI not connected yet. `scanned` is an install in use that has run one scan of the made-up job board, `scanned, signed out` is the same after its Claude login has expired, and `scanned after a job site` is a scanned install where Adzuna had already found one of the board's roles, `scanned with a role only a job site has` is a scanned install with a role JSearch found on JobLeads at a company whose board Gus doesn't read, and `scanned with a board that says no` is a scanned install with a second company whose board has answered "slow down" for three days. `demo` is the demo below.
- `check`: `open <screen>`, `see "text"`, `not see "text"`, `status bar says "text"`, `status bar doesn't say "text"`, `menu says "text"`, `banner says "text"`, `no banner`, `click "text"`, `click "text" in the status bar`, `click "text" in the progress`, `click "text" in the message`, `progress says "text"` (waits for a whole scan), `no progress`, `pick "chip"`, `"chip" is picked`, `"text" appears before "other text"`, `type "text" into "label"`, `drag "role" to "stage"` (on the Pipeline board or list), `Gus stops answering`, `the page is refreshing slowly`, `the refresh arrives`, `message says "text"`, `Claude moves "role title" to <stage>, saying "reason"` (over MCP, with Let Claude make changes on), `Claude points "role title" at "link", saying "reason"` (the same way), `reload`.
- `judge`: a question for an AI reviewer. It isn't run yet.

The made-up roles are in `fake/sitecustomize.py` and their scores are in `fake/claude`. This repo is public, so keep cases
to made-up data.

## The demo

To show Gus to someone without showing your own search:

```bash
python3 -m evals.demo
```

It starts a separate copy of Gus, as it is in this folder now, on http://127.0.0.1:8585, filled with a made-up search:
roles at every stage, companies, news, people, this week's moves. Its menu says "Demo data". Nothing of yours is in it,
job boards and Claude are the made-up ones in `fake/`, and Check roles finds two new roles the first time. Every start
begins from the same search, with dates counted back from today; stopping it (Ctrl-C) deletes the copy. After changing
Gus, start it again to show the change.

The search is in `demo.py`. `tests/test_demo.py` fails when it no longer matches `docs/objects.md`, so a new or renamed
field has to reach the demo too.

To run the checks on your own machine:

```bash
python3 -m venv .venv && .venv/bin/pip install -r evals/requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python -m evals.browser
```
