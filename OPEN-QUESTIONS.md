# Open questions

Decisions and follow-ups to pick up later. Code only: nothing personal belongs here.

## Decide first

- **Email access.** Gus doesn't read email. Connecting it through the Claude connector was built and tested, then parked, because it gives an always-on machine access to a mailbox. The work is on the `gmail-connector` branch (closed PR #16). Options: leave email out and log replies by hand, have a session on the main computer send outcomes only, or revisit the connector.
- **What the Pi's Claude login can reach.** Logging in with a Claude account brings that account's connectors (mail, files, calendar) to the Pi, and only Gus's own agents are limited. Options: use an Anthropic API key on the Pi (pay per use), or a separate Claude account with nothing connected.
- **What should depend on the laptop.** Today deploys run from it, evals copy role data to it, and application status is sent from it. Proposed rule: the Pi runs everything and the laptop is only a screen, plus whatever local apps documents need.
- **Scoring evals.** Decide what evals should cover before more labelling. The job-first labelling change is on a branch (closed PR #17). Labels are incomplete, and the pass marks haven't been checked against a real run.
- **How much should a company's name count?** If brand is a real reason to apply, list preferred companies in the profile and have `prompts/triage.md` weigh them openly. If not, keep labels and scoring on fit alone. Comparing old labels with a relabelled set should help.
- **Separate company fit score?** One score mixes company fit and role fit, so a good role type at a strong company can still score low, and thresholds at 7 and 8 hide it. Options: keep one score, or score company and role separately and let the candidate choose which matters for each role type. Decide after a few weeks of real decisions. Relates to the company name question above and to replacing the A/B/C tiers.

## Check next

- **First real full run.** Full run has only been tested with stand-ins. Press it and watch each step. Consider moving agent runs to a cheaper model first.
- **Unread signals.** Discovery hasn't run on the Pi yet, so signals have built up.
- **Screens not yet clicked through in a browser:** the Skipped and Closed ads filters, Restore, Quick check feedback, Open folder, and the phone layout.
- **Messaged button.** One click was reported that didn't reach the Pi, and the cause wasn't found.
- **Stale moves.** Moves planned before the signal date fix may rest on old roles. Review or skip them.
- **Closed ads.** Confirm detection on real scans, then decide whether reposted roles should link to the original.

## Security

- Set a cockpit access token.
- Turn off SSH password logins on the Pi.
- Back up `data/` off the Pi: a nightly copy, with its status on the Health page. Today it exists only on the SD card.
- The Pi's disk isn't encrypted, so keys and data are readable from the card.
- Check the Claude account's extra usage setting before leaving the daily run on.

## Build next

- **Manual edits in the cockpit:** add a role from a link, edit a move's draft, add notes, correct a score, add a person or a signal.
- **Role page** with the saved job description, and CV tailoring from it. Decide the output format.
- **Drafts for follow-ups,** and the same reasoning shown for moves.
- **Bring in the network:** import connections, and fill past employers so routes work.
- **Scoring calibration** from shortlist and skip decisions.
- **"Applied AI" in title matching** may be too broad. Decide whether to narrow it.

## Sources

- **Adzuna and JSearch trial.** A 14-day quality trial has been run, and nothing from it is in live Gus. Adzuna passed the proposed bar. JSearch isn't proven, because five searches gave no results or didn't run, but both are now built in and off until keys are set. Still to decide:
  - Rerun the five missing JSearch searches, using up to 20 free requests?
  - Is the proposed bar right: at least five new 7+ roles, at least three good new companies, and under one in ten junk? Should "new 7+ roles" count only roles the candidate would also consider?
  - Adzuna's limits in Settings are unconfirmed defaults: change them if the account differs.
  - Should the company check rank roles lower rather than remove them?
  - Adzuna only gives short job extracts, so are its scores reliable enough?
- **Big company careers sites.** SuccessFactors boards are read from the sitemap each site publishes. Workday boards are read from the job data behind each careers site (`/wday/cxs/`), as its sitemap stops at 100 jobs. Gus asks only for jobs in the candidate's locations, using the board's own location filter, so a big board takes about a dozen requests; a board with no location filter is reported in Health and not read. Every request is checked against the site's robots.txt and kept to a low rate, and a site that refuses is listed in Health and left alone. Other in-house sites can't be scanned, so job alert emails (which depend on the email decision) or adding roles by hand remain the options there.
  - **Teamtailor.** Each site publishes a public feed of its open jobs at `/jobs.json` and lists it in its `/.well-known/api-catalog`, so one request per scan gets everything. Teamtailor's own API needs a company's secret key and isn't used. These sites' robots.txt allows the feed, and their content signal says `ai-input=yes, ai-train=no`. The feed matched the sitemap on every board checked, the largest with 56 jobs, but a much larger board or a site with several languages hasn't been tested.
  - **Workday's terms.** Its robots.txt has no rule either way for `/wday/cxs/`. The candidate chose to use it. Workday's website terms ban scraping and ignoring robots.txt, and bar using its APIs without a separate agreement. They name workday.com, its APIs and pages that reference those terms; the careers pages checked don't reference them. This hasn't had a legal check, and each company's own terms vary.
- Most VC job boards need a browser, so they're left to agents. Some sites block automated access entirely.
- Optional keys: X, Reddit, Companies House. ntfy for phone notifications isn't set up.

## Later

- Build in public: the launch post and how it's shared.
- Move hosting if the Pi stops being enough.
