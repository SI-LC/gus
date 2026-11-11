# The objects

Everything Gus knows lives in `data/`, one JSON file per object, each a dict keyed by id. Roles, events, people, moves and emails are written through
`radar/store.py` (a lock, then a whole-file replace), so the scanner and the cockpit never overwrite each other;
`companies.json` and an agent's own edits are whole-file writes without the lock. `python3 -m radar.schema check` confirms the files still match this page; the code scan runs it.

Records may carry extra fields: the research agents keep what they find. What's listed here is what the code reads.

## Role (`data/jobs.json`)

A job ad. Key: `<source prefix>:<slug>:<id on the board>`, e.g. `gh:acme:123`, `lever:…`, `hn:…`, `adzuna:…`,
`gc:search:…` and `amz:search:…` for the saved Google Careers and amazon.jobs searches, `ghc:github:…` for GitHub's own
careers site, `mtp:jobs:…` for Mind the Product's jobs board, `getro:<board host>:…` for VC boards on Getro, `email:<message id>:<n>` for roles
pulled from the inbox, `manual:<company>:<hash of the title>` for a role you added by hand. One ad is one record however many places list it: before
adding a role, every writer asks `radar.jobs.find_match` whether Gus has it already (by id, by address without tracking
tags, or by company and title among live roles found through another source) and, if so, records the sighting under
`also` instead. `python3 -m radar.jobs dedupe` shows duplicates that got in before; `--apply` merges them, keeping the
copy furthest along and moving its events and moves across.

| Field | Meaning |
|---|---|
| `company`, `title`, `url`, `location`, `description`, `posted` | From the board |
| `stage` | `new` → `shortlisted` → `preparing` → `applied` → `interviewing` → `closed`, or `skipped` |
| `history` | `[{stage, at}]`, appended on every stage change |
| `score`, `why`, `flags` | Triage: 1–10 against the profile, null until scored; under 5 goes straight to `skipped` |
| `score_before_salary` | When a stated salary lowered the score |
| `source` | `board`, `hn`, `email`, `x`, `web`, `adzuna`, `jsearch`, `manual` (added by hand) |
| `text_kind` | What `description` is: `posting` (the employer's own ad) or `summary` (a job site's précis, which scores less reliably) |
| `first_seen`, `last_seen`, `missed`, `ad_closed` | Still on the board? Two scans without it and it's closed (never a `manual` role) |
| `also` | `{other id: {url, source, seen}}`: the same ad as listed elsewhere. A role stays live while any of its ids is on a board |
| `note`, `folder` | Yours: a note and where the application files are |
| `contacts` | `{person_id: when}`: the mapped people you've messaged about this role |

A role that reached you directly, from a recruiter say, has no ad for a scan to find: add it from the Pipeline page
("+ Add a role") or POST `/api/job/add` with `company` and `title`, and optionally `location`, `url`, `description`,
`folder`, `note` and a starting `stage` (`shortlisted` unless given; not `closed` or `skipped`). It's saved with
`source: manual` and may have no `url`; a company new to Gus becomes an account the way the Companies page adds one, in the
same request: its public job board is looked for (surer with an optional `careers` address), added to the watchlist and
scanned from the next scan on. With no board found it's still an account, and the full run keeps looking. It's scored like any other role,
but it's yours: a score under 5 doesn't skip it and no scan closes it. A role Gus already has is refused, with its id.
Its `title`, `location`, `url` and `description` can be corrected afterwards (POST `/api/job` with its `id`, or "Details"
on its Pipeline card); a new description clears the score, so the next scan scores it again. Its id and company stay as
they were. A role from any other source keeps what its ad says.

POST `/api/job` also sets any role's `stage`, `note` and `folder`. GET `/api/job?id=<id>` returns one role in full,
`description` included, which `/api/state` leaves out.

A role scoring 7+ that's still open is a `role_posted` event on its account (`role:<job id>` in `data/events.json`), written by
`radar.events sync` after every scoring run and closed when the ad goes or you skip the role. A role with no `url` is
never an event: there's nothing for a move to cite.

## Account (`data/companies.json`)

A company Gus watches. Key: the company name, as it appears in `watchlist.csv`.

| Field | Meaning |
|---|---|
| `status` | `candidate` → `watched` or `maybe` or `rejected`. `vetoed: true` means never re-add |
| `tier` | `A`, `B`, `C`. Set by the watchlist agent, or derived from `fit` (8+ A, 7 B, else C). `tier_locked` when you set it |
| `fit`, `fit_why`, `assessed`, `previous_fit`, `recheck_after` | The agent's assessment and when to look again |
| `source`, `signal` | How the company was found: which agent or source, and the one-line signal that surfaced it |
| `reassess` | Set when a new signal names a watched company (the signal carries `company`); cleared when the agent reassesses it |
| `product`, `stage`, `london` (its presence in your target locations: the name is from Gus's first search), `headcount`, `customers`, `investors`, `product_leaders`, `careers_url` | Research. `careers_url_broken` when the address no longer opens |
| `board`, `board_broken` | `<ats>:<slug>` from the watchlist; set when the last scan of it failed |
| `board_guess`, `board_guess_rejected`, `guess_checked` | A board found by name alone, `{board, url, open_roles, found}`, waiting for you to accept or reject in the cockpit; the ones you said weren't theirs; when the name was last tried |
| `careers_checked` | When an agent last read the careers page of a company with no board, looking for a board or for roles |
| `favourite`, `neighbour_of`, `neighbours_checked` | Companies you love and the ones found next to them |
| `added`, `people_checked` | Housekeeping dates |

What happened at the account lives in `data/events.json`, below. An older record's inline `events` and `pending_signals` are
migrated off it by the next scan.

Accounts come from `watchlist.csv` (`companies sync`), the watchlist agent, or you: `python3 -m radar.resolve --add` or the
Companies page in the cockpit (POST `/api/company/add` with `name` and an optional `careers` address). Both look for its
public job board first. From the Companies page a company with no board found is still an account (`source: manual`, no
`board`) until one turns up; `radar.resolve --add` only adds one whose board it found.

## Event (`data/events.json`)

Something that happened at an account which makes it worth acting on now: the reason behind a move. Key:
`<account slug>:<type>:<YYYY-MM-DD>` (`-2`, `-3`… when a day has several), or `role:<job id>` for a role.

| Field | Meaning |
|---|---|
| `account` | The account's name, as in `companies.json` |
| `type` | `post_hiring`, `role_posted`, `leader_hire`, `funding`, `london_office`, `launch`, `mention` |
| `summary`, `url`, `date` | One line, the link, and the real date (`YYYY-MM-DD`). All three required |
| `source` | Where it came from: `signal:<id>`, `job:<id>`, `agent` (an agent's own research or web search), `account` (migrated), `move` (migrated), `manual` |
| `job_id`, `closed` | On a `role_posted` event: the role, and the date its ad closed, after which it stops counting |
| `notified` | When `radar.events alert` pushed it to your phone, or `role alert` if the role's own alert covered it |
| `added` | |

Events are written by the discover agent (from a signal), the watchlist agent (from research), `radar.events sync` (from
roles scoring 7+, the threshold being `ROLE_EVENT_SCORE`) or you, with `python3 -m radar.events add` or from the account's
card in the cockpit (POST `/api/event`). The same link at the same account is one event. Age, freshness (30 days) and strength
are worked out at read time. Only **fresh** events at **A or B** accounts reach the weekly planner.

`radar.events alert`, run after every scoring run, pushes a phone alert for a `post_hiring`, `leader_hire` or `role_posted` event at
an A or B account, once per event and only for news recorded in the last 2 days, at most 5 a run. Off with the `notify_news` setting.

Dismissing an event in the cockpit sets `closed`: it stops counting but stays on record, as a move may cite it. A
`role_posted` event can't be dismissed, since it follows its role: skip the role instead.

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

One outreach for the week: one person, one fresh event, an angle and a draft. Key: `<ISO week>:<person_id>`.

| Field | Meaning |
|---|---|
| `week`, `account`, `person_id` | |
| `event_id` | The event the message is anchored on, in `events.json` |
| `why_now`, `angle`, `draft`, `channel`, `give_first` | The message and its reasoning |
| `status` | `todo`, `done`, `skipped`; `done_at` |
| `outcome` | `no_reply`, `replied`, `conversation`, `interview`, `referral`, `not_now`; `outcome_at` |

Only `python3 -m radar.accounts plan` writes moves, and it refuses any without a fresh, recorded event at an A/B
account to a person mapped there with evidence; at most 5 a week, one per account, and a 21-day cooldown per account
unless something new happened. Agents can't edit this file. Marking a move done moves its person to `contacted`; an
outcome moves them further along.

## Signal (`data/signals.json`)

The inbox: a raw item from a feed or social search, before anyone has looked at it. Key: a feed hash or `bsky:` /
`mastodon:` / `reddit:` / `x:` id.

| Field | Meaning |
|---|---|
| `source`, `kind` | Where from; `newsletter`, `signals`, `jobs`, `community`, `social`, `social-bot` |
| `title`, `text`, `url`, `date`, `name`, `handle` | The item |
| `status` | `new` until the discover agent sets `processed` or `ignored` |
| `first_seen`, `mention_checked` | Housekeeping |
| `company`, `job_id`, `event_id` | What it turned out to be about: the account it names, the role or the event it became |

A signal is never acted on itself. One that matters becomes an event (or a role) and points at it; the rest are ignored.
The funnel's first step counts fresh events, not signals.

## Everything else

- `data/emails.json`, `data/meetings.json`: filtered inbox snippets and calendar invites, processed by an agent with no internet access.
- `data/network.json`: your LinkedIn connections (name, link, company, position, date; emails dropped).
- `watchlist.csv`: the boards to scan; `companies sync` mirrors it into each account's `board`. An optional `only` column holds a pattern a role's title must match, for a company whose roles sit on its parent's board (Base44 on Wix's).
- `data/*_state.json`, `board_errors.json`, `feed_errors.json`, `heartbeat.log`: state for the scheduler, sources, feeds and X.
- `data/login_expired`: there while the last Claude run failed because the sign-in had expired (`since`, and `notified`, the day of the last push). The cockpit shows a warning and the Sign in with Claude button until a run works again, which removes it. `budget_notified` is the same for the daily AI limit.
- `data/archive/signals.json`, `data/archive/events.json`: what `radar.prune` moved out after `prune_after_days` (ignored signals that became nothing, closed events no move points at). A closed or skipped role that old loses its `description` instead and gains `pruned`; the record stays so the role can't come back as new. Roles with a note, a folder or a stage past `new` are never pruned.

## How they connect

```
signals.json ──agent──▶ events.json ◀──events sync── jobs.json (score 7+)
                            │  (account = companies.json key)
                            ▼ fresh, A/B
                      accounts plan ──▶ moves.json ──done──▶ people.json.stage
                                             ▲                     ▲
                            people.json ─────┘      network.json ──┘ (warm, distance)
```
