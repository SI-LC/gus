#!/usr/bin/env bash
# One scan, run by the scheduler or the Scan now button. Claude is only used when something is new.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p data
python3 -m radar.x >>data/x.log 2>&1
python3 -m radar.feeds >>data/feeds.log 2>&1
python3 -m radar.social >>data/social.log 2>&1
python3 -m radar.inbox >>data/inbox.log 2>&1
new=$(python3 -m radar.scan --json 2>>data/errors.log | python3 -c "import json,sys; d=json.load(sys.stdin); [print(j['company']+' — '+j['title'][:70]) for j in d['new']]")
if [[ -n "$new" ]]; then
  count=$(echo "$new" | wc -l | tr -d ' ')
  python3 -m radar.notify "Gus: $count new role(s)" "Scoring with Claude…" "$(echo "$new" | head -3 | tr '\n' ';')"
fi
python3 -m radar.triage >>data/triage.log 2>&1  # always: scores anything waiting, costs nothing if there's none
python3 -m radar.people >>data/people.log 2>&1
python3 -m radar.companies mentions >>data/companies.log 2>&1
python3 -m radar.companies health >>data/companies.log 2>&1
python3 -c "from radar.notify import release_held; release_held()"
python3 -m radar.dashboard >/dev/null
echo "$(date -u +%FT%TZ) ok" >> data/heartbeat.log
