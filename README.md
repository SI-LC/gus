# Gus

**An agentic, account-based job search for product managers.** It watches the companies you care about, scores new roles against your CV, finds the people behind them, and plans a few well-aimed moves each week. You make the decisions and send the messages.

```
 SIGNALS     job boards · newsletters · funding news · social · email
    ↓
 ACCOUNTS    target companies, tiered A/B/C, with the people who matter
    ↓
 MOVES       at most 5 a week: one person, one fresh signal, one draft
    ↓
 YOU         edit it, send it, log what happened
    ↓
 PIPELINE    replies → conversations → interviews
```

## Why this exists

Finding roles isn't the hard part of a senior PM search. Getting real conversations with the right people is. Job alerts are noisy and blind to fit, and more applications rarely help.

So this treats a job search like account-based marketing, and doubles as a test bed for how product work is changing: **the deterministic bits are code, the judgement bits are agents, and the human makes the call.**

## How it works

Gus does one full run a day, or whenever you press **Full run**. It's one job, in a fixed order, so the parts can't drift out of sync:

1. **Scan and score:** read every watched job board, feed and social source, and email, then score anything new against your CV
2. **Read emails:** an offline agent picks out replies and roles
3. **Discover:** turn signals into companies, roles and dated events, and search the web for London PM roles
4. **Refresh accounts:** find companies through VCs, funding news and London openings, then research, score and tier them. The heaviest step, so it runs every few days
5. **Score what's new**, and map the people at strong accounts
6. **Plan moves:** pick this week's moves, once a week

Only the scanning is AI-free, so **Check roles** does just step 1. Everything is configurable in the cockpit, including the daily time, your provider and models, and a daily AI limit (0 turns AI off).

**No signal, no move.** A move is rejected in code unless it cites a dated, linked signal from the last 30 days, at an A or B account, for a mapped person with evidence, and that company hasn't been contacted recently.

## The cockpit

A small web app, and the place you work:

- **Pipeline:** conversations and applications, and the page the cockpit opens on
- **Role Inbox:** new roles by score, filtered by place and type
- **To do:** your moves, each with why now, your angle and a draft, plus funnel numbers
- **Companies:** target companies by tier, with their signals and people
- **Usage:** what AI runs use, by day and task
- **Settings:** AI provider, matching, schedule, sources, email, keys and your profile

For a role you want to pursue, ask Claude Code to prep it: it saves the job description, drafts your answers and writes an interview brief. You still press submit.

## Quick start

Python 3.9+, standard library only. Scoring and the agents need [Claude Code](https://claude.com/claude-code).

```bash
git clone https://github.com/<you>/gus && cd gus
git config core.hooksPath .githooks                 # the code-only commit guard
cp profile/profile.example.md profile/profile.md    # your CV and what you want (never committed)
python3 -m radar.scan                               # first scan; creates watchlist.csv from the example
python3 -m radar.service                            # the cockpit: http://127.0.0.1:8484
```

Then, under **Settings**, you can add keys, connect Gmail, set up [ntfy](https://ntfy.sh) phone notifications and turn on the daily run.

## Run it on a Raspberry Pi

Always on, and private to your own devices through [Tailscale](https://tailscale.com). You'll need a 64-bit Pi with Tailscale and Claude Code installed and logged in.

```bash
git clone https://github.com/<you>/gus && cd gus
sudo bash deploy/install-pi.sh
```

Turn on HTTPS certificates in the Tailscale admin console and open `https://<your-pi>.<your-tailnet>.ts.net`. In the cockpit, set that as **Cockpit address** under Settings → Notifications, then turn on the daily run under Settings → Schedule.

To update it, merge your change through a pull request, then run `bash deploy/deploy.sh` from your own machine. It scans the code, updates the Pi, checks it came back and publishes a numbered release.

## Make it yours

- **Add a company:** ask the watchlist loop to find it, or run `python3 -m radar.resolve --add "Acme"`, which works out which of six job board systems it uses.
- **Change what counts as a match:** Settings → Matching.
- **Change how roles are judged:** edit [`prompts/triage.md`](prompts/triage.md).
- **Add a source:** paste any newsletter link under Settings → Newsletters.

## Checking it still works

Every pull request installs Gus from scratch, runs a full scan against a made-up job board and a fake AI, and opens the cockpit in a real browser. The requirement checks in [`evals/cases`](evals/README.md) say what the product must do in plain words, and a failure blocks the merge.

## Checking the scoring

`tests/test_replies.py` checks the code around the model. To check the model's judgement, label some real roles yourself and compare:

```bash
python3 -m radar.evals sample   # picks about 40 roles from your Pi
python3 -m radar.evals label    # you mark each one, without seeing its score
python3 -m radar.evals run      # scores them 3 times, and fails on any good role it would have skipped
```

Run it after changing `prompts/triage.md`, the scoring model or your profile. It saves nothing to your roles, sends no alerts, and keeps your labels in `data/evals/`.

## Privacy: the repo holds code only

Your CV, watchlist, settings, keys, drafts and everything Gus finds are git-ignored. Hooks refuse any commit or push containing those files, keys, or your contact details, and every pull request must pass a code scan and a full-history secret scan before it can merge.

Your data stays on your machine. What goes out: requests to public job boards and feeds, your scoring and research prompts to your AI provider, and notifications to your ntfy topic.

## Security

Gus reads untrusted text from the web and email, so it limits what that text can do.

- **Web agents** can read this repo and search the web, but can only write to `data/`. They can't read your keys or emails, reach files outside the repo, change code, or run anything but Gus's own commands.
- **Email** is read-only and filtered before opening. A separate agent with no internet access processes it, so a crafted email can't send your data out.
- **The cockpit** listens on this machine only by default, and won't listen on a network without an access token. Keep it behind a private network such as Tailscale, never on the public internet.
- **Known limit:** web agents read your profile, so a page that tricks one could in theory leak it. They can't reach your keys or emails that way.

## Principles

1. **Code for the deterministic, agents for the judgement.** Polling is a job for code, not an LLM.
2. **Signal over volume.** Five good moves a week beat 40 roles in a queue.
3. **Warm beats cold.** The person who posted "we're hiring" is worth more than the apply button.
4. **Human in the loop where it counts.** The agent prepares; you apply and you press send. No auto-submitting or auto-DMs, ever.

## Roadmap

Open decisions and next steps are in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

## License

[MIT](LICENSE)
