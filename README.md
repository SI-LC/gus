# PM Radar

**An agentic job search for product managers.** It watches the job boards of the companies you care about, tells you within minutes when a relevant role opens, and has Claude score it against your CV before you've finished reading the notification.

```
 every 10 min            only when something is new         when you say so
┌──────────────┐        ┌─────────────────────────┐        ┌──────────────────────┐
│  scan        │  new?  │  triage (Claude)        │  ≥8?   │  prep (Claude Code)  │
│  19 boards   │──────▶ │  score 1–10 vs your CV  │──────▶ │  JD saved, answers   │
│  + HN hiring │        │  one-line why + flags   │ notify │  drafted, team intel │
└──────────────┘        └─────────────────────────┘        └──────────────────────┘
   plain Python            headless `claude -p`               you still hit submit
   free to run             a few calls a day
```

## Why this exists

Good PM roles fill fast, and the best ones are visible on the company's own board hours or days before they reach LinkedIn. Job alerts are noisy, keyword-based and blind to fit.

This project uses the job market as a test bed for how product work is changing: **the deterministic bits are code, the judgement bits are an agent, and the human makes the call.**

## What it does

| Stage | How | Cost |
|---|---|---|
| **Discover** | An hourly agent ([`prompts/discover.md`](prompts/discover.md)) harvests signals from newsletters, funding news, HN, X and Bluesky, sweeps every Ashby/Greenhouse/Lever/Workable board on the web for London PM roles, and adds new companies itself | Agent run, hourly |
| **Resolve** | `radar.resolve` works out which public job board any company uses (6 systems) and adds it to the watchlist | Free |
| **Enrich** | The agent profiles each company: stage, last round, London presence, product leaders, recent signals | Agent run |
| **Watch** | Polls every watched board plus HN "Who is hiring", RSS feeds ([`feeds.csv`](feeds.csv)) and X every 10 min | Free (X billed per post, capped) |
| **Filter** | Title and location regex in [`config.json`](config.json) | Free |
| **Notify** | Native macOS notification the moment a matching role appears | Free |
| **Triage** | Claude scores each new role 1–10 against `profile/profile.md` with a one-line rationale and red flags | Only runs on new roles |
| **Find the people** | For roles scoring 7+, Claude finds who's announcing the role or hiring for it (Bluesky API, plus web search over public X, LinkedIn, Threads and blog posts) and drafts a short DM to each | A few calls per strong match |
| **Prep** | In Claude Code: "prep the Synthesia enterprise role" saves the job description, drafts application answers in your voice and researches the team | On demand |

Inbox-only sources such as newsletter job posts, LinkedIn alerts and Slack #jobs digests are read by Claude through the Gmail connector. See [CLAUDE.md](CLAUDE.md).

## Quick start

```bash
git clone https://github.com/<you>/pm-radar && cd pm-radar
cp profile/profile.example.md profile/profile.md   # your CV + what you want (never committed)
python3 -m radar.scan                               # first scan; creates watchlist.csv from the example
python3 -m radar.server                             # the cockpit: http://127.0.0.1:8484
./install.sh                                        # scan every 10 min in the background
```

Python 3.9+, standard library only. Scoring and the agent need [Claude Code](https://claude.com/claude-code).

**Phone notifications:** install [ntfy](https://ntfy.sh), subscribe to a long random topic name, and put it in `.ntfy_topic`.

**The cockpit** is where you work: new roles (shortlist or skip), the pipeline, the outreach queue with drafted
messages, the watchlist (remove companies you don't want) and radar health.

## Make it yours

**Add a company.** Add a line to `watchlist.csv` (your copy of [`watchlist.example.csv`](watchlist.example.csv)), or let the watchlist loop do it:

```csv
company,ats,slug,sector
Monzo,greenhouse,monzo,fintech
```

Find the slug in the company's careers URL: `boards.greenhouse.io/<slug>`, `jobs.ashbyhq.com/<slug>` or `jobs.lever.co/<slug>`.

**Change what counts as a match.** Edit the regexes in [`config.json`](config.json).

**Change how roles are judged.** Edit [`prompts/triage.md`](prompts/triage.md). It's the most opinionated file in the repo.

## Privacy: the repo holds code only

Your CV (`profile/profile.md`), your watchlist (`watchlist.csv`), drafts (`content/`), tokens and
everything the radar finds (`data/`) are git-ignored. A pre-commit guard (`.githooks/pre-commit`)
refuses any commit that includes those files, or that contains an email address or phone number from your profile.
Nothing leaves your machine except calls to public job APIs, your ntfy topic, and Claude.

## Security

The radar reads untrusted text from the web, so it's built to limit what that text can do.

- **Agent runs** can read this repo and search the web, but can only write to `data/`. They can't read your keys or files outside the repo, can't change code, and can only run the radar's own commands.
- **The cockpit** listens on this machine only by default, and won't listen on a network without an access token. Keep it behind a private network such as Tailscale; don't expose it to the internet.
- **Known limit:** an agent run reads your profile and can fetch web pages, so a page that tricks it could in theory leak what's in your profile. Treat `profile/profile.md` as something an agent may see. The agent can't reach your keys this way.

## Principles

1. **Code for the deterministic, agents for the judgement.** Polling is a cron job, not an LLM call.
2. **Signal over volume.** One 9/10 notification beats fifty "new jobs for you" emails.
3. **Human in the loop where it counts.** The agent prepares, you apply and you hit send. No auto-submitting or auto-DMs, ever.
4. **Warm beats cold.** The person who posted "we're hiring" is worth more than the apply button.
