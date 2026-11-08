# The objects

Everything Gus knows lives in `data/`, one JSON file per object, each a dict keyed by id. Files are written through
`radar/store.py` (a lock, then a whole-file replace), so the scanner, the agents and the cockpit never overwrite each
other. `python3 -m radar.schema check` confirms the files still match this page; the code scan runs it.

Records may carry extra fields: the research agents keep what they find. What's listed here is what the code reads.

## Role (`data/jobs.json`)

A job ad. Key: `<source prefix>:<slug>:<id on the board>`, e.g. `gh:acme:123`, `lever:…`, `hn:…`, `adzuna:…`,
`email:<message id>:<n>` for roles pulled from the inbox.

| Field | Meaning |
|---|---|
| `company`, `title`, `url`, `location`, `description`, `posted` | From the board |
| `stage` | `new` → `shortlisted` → `preparing` → `applied` → `interviewing` → `closed`, or `skipped` |
| `history` | `[{stage, at}]`, appended on every stage change |
| `score`, `why`, `flags` | Triage: 1–10 against the profile, null until scored; under 5 goes straight to `skipped` |
| `score_before_salary` | When a stated salary lowered the score |
| `source` | `board`, `hn`, `email`, `x`, `web`, `adzuna`, `jsearch` |
| `first_seen`, `last_seen`, `missed`, `ad_closed` | Still on the board? Two scans without it and it's closed |
| `note`, `folder` | Yours: a note and where the application files are |
| `contacts` | `{person_id: when}`: the mapped people you've messaged about this role |

A role scoring 7+ that's still open becomes a `role_posted` event on its account, derived at read time, never stored.

## Account (`data/companies.json`)

A company Gus watches. Key: the company name, as it appears in `watchlist.csv`.

| Field | Meaning |
|---|---|
| `status` | `candidate` → `watched` or `maybe` or `rejected`. `vetoed: true` means never re-add |
| `tier` | `A`, `B`, `C`. Set by the watchlist agent, or derived from `fit` (8+ A, 7 B, else C). `tier_locked` when you set it |
| `fit`, `fit_why`, `assessed`, `previous_fit`, `recheck_after` | The agent's assessment and when to look again |
| `events` | `[{type, summary, url, date}]`. What makes the account worth acting on now. See below |
| `pending_signals`, `reassess` | A watched company named in a new signal; cleared when the agent reassesses it |
| `product`, `stage`, `london`, `headcount`, `customers`, `investors`, `product_leaders`, `careers_url` | Research |
| `board`, `board_broken` | `<ats>:<slug>` from the watchlist; set when the last scan of it failed |
| `favourite`, `neighbour_of`, `neighbours_checked` | Companies you love and the ones found next to them |
| `added`, `people_checked` | Housekeeping dates |

Event types: `post_hiring`, `role_posted`, `leader_hire`, `funding`, `london_office`, `launch`, `mention`. Every event needs a
link and a real date. Only **fresh** events (30 days) at **A or B** accounts reach the weekly planner.

## Person (`data/people.json`)

Someone mapped at an account. Key: `<name slug>@<company slug>`, e.g. `sam-jones@acme`.

| Field | Meaning |
|---|---|
| `name`, `company`, `role`, `platform`, `url` | Who, where, and their public profile |
| `kind` | `product_leader`, `hiring_manager`, `peer_pm`, `recruiter`, `warm`, `announcer` |
| `evidence`, `evidence_date`, `confidence`, `past_companies` | Why we think they're there; `past_companies` feeds network distance |
| `warm`, `warm_how` | A 1st-degree LinkedIn connection, from your Connections export |
| `degree`, `via`, `degree_at` | What you confirmed on their LinkedIn profile by hand |
| `stage` | `mapped` → `contacted` → `replied` → `conversation` → `referred` → `closed` |
| `history`, `note`, `added`, `source` | |

People come from the people search (`source: people search`) or are added by hand (`source: manual`), with
`python3 -m radar.accounts add-person "Name" "Company"` or POST `/api/person` with `name` and `company` and no `id`.
The company must already be an account.

Network distance (1st, ~2nd, 3rd+) is worked out at read time from `data/network.json`, never stored.

## Move (`data/moves.json`)

One outreach for the week: one person, one fresh signal, an angle and a draft. Key: `<ISO week>:<person_id>`.

| Field | Meaning |
|---|---|
| `week`, `account`, `person_id` | |
| `signal` | `{type, summary, url, date}`: the event the message is anchored on |
| `why_now`, `angle`, `draft`, `channel`, `give_first` | The message and its reasoning |
| `status` | `todo`, `done`, `skipped`; `done_at` |
| `outcome` | `no_reply`, `replied`, `conversation`, `interview`, `referral`, `not_now`; `outcome_at` |

Only `python3 -m radar.accounts plan` writes moves, and it refuses any without a fresh, dated, linked signal at an A/B
account to a person mapped there with evidence; at most 5 a week, one per account, and a 21-day cooldown per account
unless something new happened. Agents can't edit this file. Marking a move done moves its person to `contacted`; an
outcome moves them further along.

## Signal (`data/signals.json`)

A raw item from a feed or social search, before anyone has looked at it. Key: a feed hash or `bsky:` / `mastodon:` /
`reddit:` / `x:` id.

| Field | Meaning |
|---|---|
| `source`, `kind` | Where from; `newsletter`, `signals`, `jobs`, `social`, `social-bot` |
| `title`, `text`, `url`, `date`, `name`, `handle` | The item |
| `status` | `new` until the discover agent sets `processed` or `ignored` |
| `first_seen`, `mention_checked`, `company`, `job_id` | Housekeeping and what it turned out to be about |

A signal about an account is promoted into that account's `events`. "Signal" in the funnel means a fresh event.

## Everything else

- `data/emails.json`, `data/meetings.json`: filtered inbox snippets and calendar invites, processed by an agent with no internet access.
- `data/network.json`: your LinkedIn connections (name, link, company, position, date; emails dropped).
- `watchlist.csv`: the boards to scan; `companies sync` mirrors it into each account's `board`.
- `data/*_state.json`, `board_errors.json`, `feed_errors.json`, `heartbeat.log`: state for the scheduler, sources, feeds and X.

## How they connect

```
signals.json ──agent──▶ companies.json.events ◀──derived── jobs.json (score 7+)
                              │
                              ▼ fresh, A/B
                        accounts plan ──▶ moves.json ──done──▶ people.json.stage
                                               ▲                     ▲
                              people.json ─────┘      network.json ──┘ (warm, distance)
```
