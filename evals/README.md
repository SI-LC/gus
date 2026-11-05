# Requirement checks

Each file in `cases/` is one thing the product must do, in plain words. Every pull request runs them in a real browser
against a fresh install with made-up data, and a failure blocks the merge. Any script error on the page fails the case too.

```yaml
id: shortlist-a-role
requirement: When I shortlist a role, it moves to my pipeline and stays there.
given: scanned
check:
  - open Roles
  - click "Shortlist"
  - open Pipeline
  - see "Senior Product Manager"
judge: Is it clear the role has moved, and where to find it?
```

- `given`: `empty` is a brand new install, and `scanned` has run one scan of the made-up job board.
- `check`: `open <screen>`, `see "text"`, `not see "text"`, `click "text"`, `pick "chip"`, `"chip" is picked`, `"text" appears before "other text"`, `reload`.
- `judge`: a question for an AI reviewer. It isn't run yet.

The made-up roles are in `fake/sitecustomize.py` and their scores are in `fake/claude`. This repo is public, so keep cases
to made-up data.

To run them on your own machine:

```bash
python3 -m venv .venv && .venv/bin/pip install -r evals/requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python -m evals.browser
```
