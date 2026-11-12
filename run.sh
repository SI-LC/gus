#!/usr/bin/env bash
# One scan: the Check roles button, and the first step of a full run. AI is only used when something is new.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p data
stage() { echo "$1" > data/doing.txt; }  # what the cockpit shows as happening now
stage "Reading X"
python3 -m radar.x >>data/x.log 2>&1
stage "Reading news feeds"
python3 -m radar.feeds >>data/feeds.log 2>&1
stage "Reading social posts"
python3 -m radar.social >>data/social.log 2>&1
stage "Reading email"
python3 -m radar.inbox >>data/inbox.log 2>&1
stage "Reading job boards"
new=$(python3 -m radar.scan --json 2>>data/errors.log | python3 -c "import json,sys; d=json.load(sys.stdin); [print(j['company']+' — '+j['title'][:70]) for j in d['new']]")
if [[ -n "$new" ]]; then
  count=$(echo "$new" | wc -l | tr -d ' ')
  python3 -m radar.notify "Gus: $count new role(s)" "Scoring with Claude…" "$(echo "$new" | head -3 | tr '\n' ';')"
fi
stage "Scoring new roles"
python3 -m radar.triage >>data/triage.log 2>&1  # always: scores anything waiting, costs nothing if there's none
stage "Mapping people at companies with news"
python3 -m radar.people >>data/people.log 2>&1
stage "Tidying up: companies, events and old roles"
python3 -m radar.companies sync >>data/companies.log 2>&1  # new boards reach the Companies screen straight away
python3 -m radar.companies mentions >>data/companies.log 2>&1
python3 -m radar.companies health >>data/companies.log 2>&1
python3 -m radar.events sync >>data/companies.log 2>&1  # roles scoring 7+ become events; closed ones stop counting
python3 -m radar.prune --apply >>data/companies.log 2>&1  # old closed roles lose their text; old ignored signals and closed events are archived
python3 -c "from radar.notify import release_held; release_held()"
echo "$(date -u +%FT%TZ) ok" >> data/heartbeat.log
