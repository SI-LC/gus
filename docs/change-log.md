# Spec: a log of changes to your roles

Status: built on the `change-log` branch, with the eval case and tests passing. Not yet merged or deployed.

## The problem

Gus changes your roles, and some of those changes are made by Claude or by triage rather than by you. Today the only
record is a role's `history`, which keeps stage changes and nothing else:

- **Notes are overwritten.** `update_job` replaces `note` in full. A bad write, from you or Claude, loses the old text
  for good. The same goes for `skip_reason` and `folder`.
- **Who made a change, and why, isn't recorded.** A `history` entry says `{stage, at}`.
- **Triage's automatic skip leaves no trace at all.** It sets `stage` to skipped without adding to `history`, so for
  most skipped roles "why is this skipped?" can't be answered.
- **"What changed today?" can't be answered in one place.** Claude has to list every skipped role (the list stops at
  200 of over 1,000) and open them one by one.

This matters more once agents run unattended: you need to see what they did, when and why.

## What you'll be able to do

1. See in Activity what changed on your roles, newest first, who changed it and why.
2. See the old value of a change, including the full old text of a replaced note, so nothing is lost.
3. Know why Claude changed something: every write by Claude gives a reason.
4. Ask Claude "what changed since yesterday?" and get the answer in one call.

## Where roles are written today

Only two places change a role's `stage`, `skip_reason`, `note` or `folder`:

- `server.update_job` ([server.py](../radar/server.py)), used by the cockpit and, through `mcp.update_role`, by Claude.
- The automatic skip in `triage` (a score under 5 on a new role).

`jobs dedupe` only fills fields that are empty on the role it keeps, prune moves roles to the archive, and `links` and
`recall` don't write these fields. None of them overwrite a decision, so they aren't logged.

## Design

### The log

`data/changes.jsonl`: one JSON object per line, appended under the jobs store's lock, never edited.

| Field | Meaning |
|---|---|
| `id` | `chg_<time>_<4 random chars>`, sortable |
| `at` | ISO time, UTC |
| `role` | The role's id, e.g. `gh:acme:123` |
| `field` | `stage`, `skip_reason`, `note` or `folder` |
| `old`, `new` | The values before and after |
| `actor` | `you` (cockpit), `claude` (MCP) or `triage` |
| `reason` | Why, in a sentence. Required from Claude |

`history` on a role stays as it is: what you did to its stage. Triage's skip goes in the log and not in `history`,
because the cockpit reads a skip in `history` as "You skipped this" (changed while building).

### Writing it

- `update_job` takes an `actor` and a `reason`, compares old with new and appends one line per field that changed. The
  cockpit passes `you`; `mcp.update_role` passes `claude` and its `reason`.
- Triage appends a line for each role it skips, with the score as the reason ("scored 3/10").

### Reading it

- **MCP:** `list_changes(since?, until?, who?, role_id?, limit?)`, newest first. Each change carries the role's company and title, so
  it reads without opening the role.
- **Cockpit:** changes appear as lines in the existing Activity page ("Claude moved *Senior PM @ Acme* from applied to
  skipped: ad closed"). A replaced note shows its old text.
- `docs/objects.md` and `radar.schema check` cover the log's fields.
- The log holds what your data holds, notes included, so it is kept and backed up with `data/`.

## Out of scope for the first version

- Undo, from the cockpit or MCP. Claude can put a value back with `update_role`, reading the old value from the log.
- A Changes view with its own filters, a CLI, and a change to the 200 cap.
- Backfilling the log from today's `history`, and monthly roll-over of the file.
- Edits to the details of roles you added by hand (title, company, location, link, description).
- Changes to companies, events, people and moves.
- Writes that never reached the Pi. A log on the Pi can't see them; that's a separate fix.

## Decisions to make (step 2)

Settled in `/grill-me`. Round 1, confirmed:

1. **Store:** JSONL in `data/changes.jsonl`, appended under the jobs lock. Revisit only if `list_changes` gets slow.
2. **No reason from Claude:** refused, with "Needs reason", as for any tool's missing field; the tool description says
   it's required and that you see it in Activity.
3. **Triage skips:** one line per role skipped, reason "scored N/10".
4. **Your own changes:** shown in Activity alongside Claude's and triage's.
5. **Cockpit:** a separate `/api/changes`, fetched when Activity opens; `/api/state` doesn't carry the log.
6. **`list_changes` default:** the last 24 hours, newest first, at most 200, saying when there are more.
7. **Actor:** every MCP write is `claude`; the reason carries the context. Agent names come with undo by run.

Round 2, confirmed:

8. **Place:** a "What changed" section on Activity, after "Is everything working?" and before what Claude cost.
9. **How far back:** the last 7 days, under day headings (Today, Yesterday, Thu 12 Nov). No "Show older".
10. **A busy triage run:** a day's triage skips are one line, "Triage skipped 40 roles scoring under 5", that opens to
    list them. Your changes and Claude's are a line each.
11. **A replaced note:** "Claude replaced your note on *Senior PM @ Acme*: <reason>", with the old text below, cut to
    2 lines and shown in full on a click. No Copy button.
12. **The role:** its title on each line opens the role: on your board, as its pipeline card does; any other role
    shows its description, as only Pipeline has a role panel.
13. **A quiet week:** "Nothing changed on your roles this week.", using the kit's empty state.

## The bar (eval case to add first)

```yaml
id: see-what-changed
requirement: I can see what changed on my roles this week, who changed it and why, newest first.
given: scanned
check:
  - Claude moves "Senior Product Manager" to applied, saying "you applied on Friday"
  - Claude moves "Senior Product Manager" to skipped, saying "ad closed"
  - open Activity
  - see "What changed"
  - see "applied"
  - see "skipped"
  - see "Claude"
  - see "ad closed"
  - '"ad closed" appears before "you applied on Friday"'
  - '"Job boards" appears before "Claude moved"'
  - '"Claude moved" appears before "Claude today"'
judge: Can I tell at a glance what changed today, who did it and why, without opening each role?
```

Plus unit tests: a cockpit write, a Claude write and a triage skip each append a line; a replaced note keeps its old
text in `old`; a Claude write without a reason is refused.

## Later

- Undo one change, then a whole agent run.
- The same log for companies, events, people and moves.
- A daily digest, e.g. "agents made 14 changes, 2 to look at".
- Approval rules: agents may skip a role, but a change to a role you're interviewing for waits for you.
- A "Changes" section on each role's page.
- Agents start each run from `list_changes(since=last run)`.
