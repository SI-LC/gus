# Gus

**An agentic, account-based job search.** It watches the companies you care about, notices what happens at each, scores new roles against your CV, finds the people behind them, and plans a few well-aimed moves each week. You make the decisions and send the messages.

```
 SIGNALS     raw items from job boards · job search sites · newsletters · funding news · social · email
    ↓
 ACCOUNTS    target companies, tiered A/B/C, with the people mapped at each
    ↓
 EVENTS      what happened there, dated and linked: funding, a new leader, a post about hiring, a strong role
    ↓
 MOVES       at most 5 a week: one person, one fresh event, one draft
    ↓
 YOU         edit it, send it, log what happened
    ↓
 PIPELINE    replies → conversations → interviews
```

The objects behind this, and the fields each one carries, are in [`docs/objects.md`](docs/objects.md).

## Why this exists

Finding roles isn't the hard part of a senior job search. Getting real conversations with the right people is. Job alerts are noisy and blind to fit, and more applications rarely help.

So this treats a job search like account-based marketing, and doubles as a test bed for how product work is changing: **the deterministic bits are code, the judgement bits are agents, and the human makes the call.**

## How it works

Gus does one full run a day, or whenever you press **Full run**. It's one job, in a fixed order, so the parts can't drift out of sync:

1. **Scan and score:** read every watched job board, feed and social source, and email if connected, then score anything new against your CV
2. **Job sources:** if you've turned them on, search Adzuna and JSearch for roles beyond your company list, within the limits you set
3. **Read emails:** if connected, an offline agent picks out replies and roles
4. **Discover:** turn signals into companies, roles and dated events, and search the web for your target roles
5. **Find boards:** work out the job board behind each company that arrived with a role or a careers address, and scan the new ones
6. **Refresh accounts:** find companies through VCs, funding news and new local openings, then research, score and tier them. The heaviest step, so it runs every few days
7. **Score what's new**, and map the people at strong accounts
8. **Plan moves:** pick this week's moves, once a week

Scanning uses no AI. Scoring does, but only for new roles, and so does mapping people at an account with fresh news. **Check roles** does just step 1. While either runs, a strip at the top of the cockpit says what it's doing right now and how long it has taken, and when it ends, what it found. Everything is configurable in the cockpit, including the daily time, your provider and models, and a daily AI limit (0 turns AI off).

**No event, no move.** A move is rejected in code unless it cites a dated, linked event from the last 30 days, at an A or B account, for a mapped person with evidence, and that company hasn't been contacted recently. Raw signals never count on their own: an agent has to turn one into an event first.

## The cockpit

A small web app, and the place you work. Each page is one object from the list above:

- **Role Inbox:** new roles scored against your profile, best first, filtered by place and type. Shortlist or skip each one. A company with three or more roles shows its best, with the rest a click away and one button to skip them all; the same role in several places is one row; and a role at a company where you already have one in play says so. A role you're pursuing that is advertised again joins your application in Pipeline, marked, and doesn't come back as new
- **Pipeline:** every conversation and application by stage, and the page the cockpit opens on
- **To do:** this week's moves, each with the event behind it, your angle and a draft, plus the funnel from events to interviews
- **Companies:** your accounts by tier, with what's happened at each and who to talk to there. Add a company, set its tier, mark the ones you love, add news Gus missed or dismiss news that isn't
- **Usage:** what the AI runs have used, by day and by task, and under it Health: the last scan, job source limits, and any board or feed that failed
- **Settings:** AI provider, matching, schedule, job sources and their limits, newsletters, email, keys and your profile

For a role you want to pursue, ask Claude Code to prep it: it saves the job description, drafts your answers and writes an interview brief. You still press submit.

## Use Gus from Claude

Gus is also an MCP server, so Claude Code or the Claude desktop app can read your search and, if you allow it, act on it: "what's new in Gus", "who do I know at Acme", "I applied to the Acme role". On the computer Gus runs on, connect it once:

```bash
claude mcp add --transport http gus http://127.0.0.1:8484/mcp
```

From another device, such as a laptop reaching a Pi over Tailscale, open **Settings → Claude** in the cockpit and press **Create token**. Gus shows the full command once, with the token in it, and keeps only a fingerprint of the token.

The Claude desktop app can't connect to an address, only start a program, so Gus comes with one that passes its messages on: `python3 -m radar.mcp --url <cockpit>/mcp`. **Settings → Claude** shows the lines to add to the app's config file (Settings → Developer → Edit Config in the app), with the token in them when you've just made one. The app needs a copy of Gus on the same computer to start the program from.

There are 19 tools and they mirror the cockpit: `list_`, `get_`, `create_` and `update_` for roles, companies, events, people and moves, plus `get_run`, `create_run` and `get_profile`. Claude can only read until you turn on **Let Claude make changes**. It can never see or change your settings or keys, rewrite your profile, or make a move: only the weekly planner does that.

## Quick start

Python 3.9+, standard library only. Scoring and the agents need [Claude Code](https://claude.com/claude-code) or an Anthropic API key. An OpenAI-compatible provider can score roles, but can't run the agents.

```bash
git clone https://github.com/<you>/gus && cd gus
git config core.hooksPath .githooks                 # the code-only commit guard
python3 -m radar.service                            # the cockpit: http://127.0.0.1:8484
```

Open the cockpit, paste your CV and tap a few companies you love. Gus works out what you're looking for, checks live roles at the starter companies in `starter-boards.csv`, and shows your top three with why each suits you and who to talk to. Those companies are mostly technology firms, so for other kinds of work add a job source in Settings.

After that, under **Settings**, you can add keys, connect Gmail, set up [ntfy](https://ntfy.sh) phone alerts for strong roles and hiring news at your target companies, and turn on the daily run.

## Run it on a Raspberry Pi

Always on, and private to your own devices through [Tailscale](https://tailscale.com). You'll need a 64-bit Pi with Tailscale and Claude Code installed and logged in.

```bash
git clone https://github.com/<you>/gus && cd gus
sudo bash deploy/install-pi.sh
```

Turn on HTTPS certificates in the Tailscale admin console and open `https://<your-pi>.<your-tailnet>.ts.net`. In the cockpit, set that as **Cockpit address** under Settings → Notifications, then turn on the daily run under Settings → Schedule.

To update it, merge your change through a pull request, then run `bash deploy/deploy.sh` from your own machine. It scans the code, updates the Pi, checks it came back and publishes a numbered release.

## Make it yours

- **Add a role by hand:** for a role that reached you directly, from a recruiter say, with no public ad for a scan to find. Open "+ Add a role" on the Pipeline page and paste the job description. Gus scores it like any other role, but never skips or closes it for you.
- **Add a company:** type its name under Companies in the cockpit, or run `python3 -m radar.resolve --add "Acme"`. Either way Gus works out which of ten job board systems it uses. Giving its careers address is surer, and companies on SuccessFactors, Workday, Teamtailor or Gem need it: Gus never guesses their address. A company whose roles sit on its parent's board (Base44 on Wix's) takes the parent's slug and a pattern its titles must match in the `only` column of `watchlist.csv`.
- **Change what counts as a match:** Settings → Matching.
- **Change how roles are judged:** edit [`prompts/triage.md`](prompts/triage.md).
- **Add a source:** paste any newsletter link under Settings → Newsletters, or add keys and turn on Adzuna and JSearch under Settings → Job sources. Google, Amazon, Microsoft and Apple have no job board to read, so paste a search from each one's careers site there too: Gus reads it once a day, newest first. Add the company under Companies as well, so its roles roll up to an account. GitHub also hires on its own site, github.careers: give that as its careers address under Companies and Gus reads the whole list once a day. Turn on **Mind the Product jobs** there too for product roles from many companies: each role arrives under its own company. Paste VC portfolio boards run on Getro (careers.atomico.com/jobs, jobs.pointnine.com/jobs) under **VC job boards** the same way.
- **Follow your own VCs:** the research agents work from `vcs.csv`, which starts as a copy of `vcs.example.csv` and is yours to edit: one VC per line, with what it focuses on and its portfolio job board if it has one. Like `watchlist.csv`, it's never committed.

## Checking it still works

Every pull request installs Gus from scratch, runs a full scan against a made-up job board and a fake AI, and opens the cockpit in a real browser. The requirement checks in [`evals/cases`](evals/README.md) say what the product must do in plain words, and a failure blocks the merge.

## Checking the scoring

`tests/test_replies.py` checks the code around the model. To check the model's judgement, label some real roles yourself and compare:

```bash
python3 -m radar.evals sample   # picks about 40 roles from your Pi (add --local to use this machine's)
python3 -m radar.evals label    # you mark each one, without seeing its score
python3 -m radar.evals run      # scores them 3 times, and fails on any good role it would have skipped
```

Run it after changing `prompts/triage.md`, the scoring model or your profile. It saves nothing to your roles, sends no alerts, and keeps your labels in `data/evals/`.

To check Gus is finding roles at all, keep a list of ones you spot yourself:

```bash
python3 -m radar.recall add "Acme" "Senior Product Manager"   # a role you saw somewhere
python3 -m radar.recall                                       # how many Gus found, and why each miss was missed
```

Each miss gets a cause: nothing reads the company, its board fell off the watchlist, your title or location rules drop the role, or it isn't on the board. Your list and the reports stay in `data/evals/`.

## Privacy

**The repo holds code only.** Your CV, watchlist, settings, keys, drafts and everything Gus finds are git-ignored. With the hooks turned on, a commit or push is refused if it contains those files, a key, or the email address or phone number in your profile. On this repo, every pull request must also pass a code scan and a full-history secret scan before it can merge. That rule is a GitHub setting, so a fork has to add its own.

**Your data is stored where Gus runs, and some of it leaves.** That's your own machine or Pi. Keys sit in a plain text file that only your user can read. Gus sends this out:

| Goes to | What's sent |
|---|---|
| Your AI provider | Your profile and CV, roles, company research and anything an agent reads, including emails if connected |
| Web search and the pages agents open | Search terms based on your target roles and locations |
| Public job boards, company careers sites and feeds | Plain requests, with nothing about you |
| Adzuna and JSearch, if turned on | Your role searches and location |
| X, Bluesky, Mastodon and Reddit, if turned on | Your search terms, and on Bluesky the names of companies whose people are being mapped |
| Gmail, if connected | Your address and app password, to read matching emails. Nothing is sent or changed |
| ntfy, if turned on | Your alerts, including role and company names. The topic name is the only protection on the public server |
| Google Fonts | Your browser's request for the cockpit's fonts |

`radar.evals sample` also copies roles from your Pi to the machine you run it on.

## Security

Gus reads untrusted text from the web and email, so it limits what that text can do.

- **Web agents** can read this repo and search the web, but can only write to `data/` and `watchlist.csv`. They can't read your keys or emails, reach files outside the repo, change code, or run anything but Gus's own commands.
- **Email** is off until you connect it. It's then read-only and filtered before opening, and a separate agent with no internet access processes it, so a crafted email can't send your data out.
- **The cockpit** has no login unless you set an access token. It listens on this machine only by default, and won't listen on a network without a token. Keep it behind a private network such as Tailscale, never on the public internet. The Pi install publishes it to your Tailscale devices, so anything on your tailnet can open it.
- **Claude's connection** (`/mcp`) is separate from the cockpit's login. From the machine Gus runs on it needs nothing. From anywhere else it needs its own token, which opens nothing but the tools, and a token made over Tailscale only works for the Tailscale login that made it. Five wrong tokens in a minute lock it for ten minutes and send you an alert. Every call is listed under Settings → Claude, by tool and target only. Role descriptions and news are written by strangers and Claude reads them through these tools, so keep **Let Claude make changes** off unless you need it, and on a shared tailnet add an access rule so only your own devices reach the Pi.
- **On other people's sites** Gus is a guest: it says who it is (`gus/<version>` with a link to this repo), follows `robots.txt` on every company careers site and job site it reads (the job board systems' public APIs are built to be read), waits between pages and between requests to one host, and reads the large sites once a day. If you run a site and would rather it didn't visit, open an issue here.
- **Known limits:** web agents read your profile, so a page that tricks one could in theory leak it. They can't reach your keys or emails that way. A Claude login can also bring that account's connectors to the machine, such as mail, files and calendar, so check what your account has connected.

## Principles

1. **Code for the deterministic, agents for the judgement.** Polling is a job for code, not an LLM.
2. **Signal over volume.** Five good moves a week beat 40 roles in a queue.
3. **Warm beats cold.** The person who posted "we're hiring" is worth more than the apply button.
4. **Human in the loop where it counts.** The agent prepares; you apply and you press send. No auto-submitting or auto-DMs, ever.

## How the code is laid out

Everything is in `radar/`, one module per job. The fields of every object in `data/` are in [`docs/objects.md`](docs/objects.md).

| Module | What it does |
|---|---|
| `service`, `server`, `cockpit.html` | The always-on service: the cockpit, its API and the daily full run |
| `mcp` | The same cockpit as tools for Claude, at `/mcp`: the naming rules, the 19 tools, and who may call them. Run as a program, the bridge the Claude desktop app starts |
| `cockpit.css`, `css` | The cockpit's look, written for [Tailwind](https://tailwindcss.com): colours and fonts are the tokens at the top of `cockpit.css`. `python3 -m radar.css` builds `static/cockpit.css` from it, and needs Tailwind's standalone program only on the machine you write code on |
| `scan`, `boards`, `web`, `matching` | One scan: read every board (`boards`), politely (`web`), keep what matches your titles and places (`matching`) |
| `sources`, `feeds`, `social`, `x`, `inbox`, `links`, `resolve` | Everything else that brings roles and signals in, and finds a company's job board |
| `jobs`, `triage`, `salary` | One role however many places list it, and its score |
| `companies`, `events`, `people`, `network`, `accounts` | Accounts, what happened at each, who's there, and the weekly moves |
| `agent`, `ai`, `usage` | The agent runs (playbooks in `prompts/`), the one way to call a model, and what it cost |
| `settings`, `paths`, `store`, `schema` | Settings and keys, where files live, the one way to read and write them, and what they must look like |
| `firstrun`, `notify`, `prune`, `evals`, `recall` | The welcome flow, phone alerts, keeping data small, checking the scoring, and checking Gus found the roles you found |

## Roadmap

Open decisions and next steps are in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

## License

[MIT](LICENSE)
