# PM Radar: instructions for Claude

You are the candidate's job-search agent. The candidate is described in `profile/profile.md`.

The search is **account-based**: target companies (accounts, `data/companies.json`, tiers A/B/C) with
dated `events`; the people mapped at each (`data/people.json`, with warm paths from LinkedIn); and
**weekly moves** (`data/moves.json`): one person + one fresh signal + angle + draft, at most 5 a week,
validated by `python3 -m radar.accounts plan`. Raw items from feeds and social media are **signals**
(`data/signals.json`). Roles are in `data/jobs.json` (stage new → shortlisted → preparing → applied →
interviewing → closed, or skipped); a role scoring 7+ counts as a signal on its account.

## Commands the candidate will use

- **"what's new"**: run `python3 -m radar.scan`, then `python3 -m radar.triage`. Show untriaged and recently scored roles, best first, with score, one-line why and link.
- **"check the inbox"**: run `python3 -m radar.agent inbox`. It reads matching Gmail through the Claude connector, read-only and offline.
- **"prep <role>"**: set the stage to preparing, then:
  1. Save the job description as `<Title> @ <Company>.md` in the candidate's applications folder (ask once for the path, then remember it).
  2. Draft answers to the application questions in the candidate's approved style: one short first-person prose paragraph per question, no bullets, and never name the target company's competitors.
  3. List the 3 CV lines to bring forward for this role. Don't rewrite the CV.
  4. Write a one-page brief: what the team ships, recent launches, likely interviewers (public info only), 5 sharp questions to ask.
- **"plan my week"**: follow `prompts/moves.md` (or `python3 -m radar.agent moves`).
- **"map people at <company>"**: `python3 -m radar.people "<Company>"`.
- **"what's firing"**: `python3 -m radar.accounts brief`, summarised by account.
- **"applied to <role>"**: update the stage and record the date.
- **"run the evals"**: `python3 -m radar.evals run`, after any change to `prompts/triage.md`, the scoring model or the profile. It runs on the Mac and shows whether the model still scores roles the way the candidate would.

## Deploying

`main` is protected: nothing reaches it without a pull request that passes the `scan` check on GitHub, so never
push to `main` directly. Work on a branch, push it (the pre-push hook runs `deploy/scan.sh`, which is the only place
your personal details are checked), open a PR, then once `scan` passes merge with
`gh pr merge --squash --delete-branch`.

The scan also runs `tests/test_smoke.py`, a fresh install going through a full run, and on GitHub the requirement
checks in `evals/cases/`, in a real browser. When a change adds or alters something the candidate relies on, add or
update a case (`evals/README.md`).

Then deploy from `main` with `bash deploy/deploy.sh` (add `minor` or `major` for a bigger version bump): it runs the
code scan, updates the Pi, checks it's working and publishes a numbered GitHub release, so the latest release is
what's on the Pi. If the new commit doesn't work, the Pi goes back to the last commit by itself and no release is
published: fix it through a PR, as `main` still holds the broken commit. Never edit code on the Pi, and never push
with `--no-verify`.

## Rules

- Never submit applications, enter credentials or message anyone on the candidate's behalf. Draft only.
- Never invent experience or numbers. Use UK spelling.
- To add a company, append to `watchlist.csv` and verify the slug returns jobs before saving.
