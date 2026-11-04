# Open questions

Decisions and follow-ups to pick up later. Code only: nothing personal belongs here.

## Decide first

- **Email access.** Gus doesn't read email. Connecting it through the Claude connector was built and tested, then parked, because it gives an always-on machine access to a mailbox. The work is on the `gmail-connector` branch (closed PR #16). Options: leave email out and log replies by hand, have a session on the main computer send outcomes only, or revisit the connector.
- **What the Pi's Claude login can reach.** Logging in with a Claude account brings that account's connectors (mail, files, calendar) to the Pi, and only Gus's own agents are limited. Options: use an Anthropic API key on the Pi (pay per use), or a separate Claude account with nothing connected.
- **What should depend on the laptop.** Today deploys run from it, evals copy role data to it, and application status is sent from it. Proposed rule: the Pi runs everything and the laptop is only a screen, plus whatever local apps documents need.
- **Scoring evals.** Decide what evals should cover before more labelling. The job-first labelling change is on a branch (closed PR #17). Labels are incomplete, and the pass marks haven't been checked against a real run.
- **How much should a company's name count?** If brand is a real reason to apply, list preferred companies in the profile and have `prompts/triage.md` weigh them openly. If not, keep labels and scoring on fit alone. Comparing old labels with a relabelled set should help.

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

- Big companies run their own careers sites, which this can't scan and whose rules may forbid it. Options are job alert emails, which depend on the email decision, or adding roles by hand.
- Most VC job boards need a browser, so they're left to agents. Some sites block automated access entirely.
- Optional keys: X, Reddit, Companies House. ntfy for phone notifications isn't set up.

## Later

- Build in public: the launch post and how it's shared.
- Move hosting if the Pi stops being enough.
