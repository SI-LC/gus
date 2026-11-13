#!/usr/bin/env bash
# Update Gus where it runs, check it works, and go back to what was there if it doesn't.
#   On the Pi, sent by deploy.sh:   bash -s <short commit> < deploy/update.sh    the commit just merged to main
#   On your own computer:           gus update                                   the latest release
set -euo pipefail

main() {  # read whole before it starts: the update replaces this file while it runs
  cd "${GUS_DIR:-$HOME/gus}"
  local target="${1:-}" url="http://127.0.0.1:${GUS_PORT:-8484}" name="Pi" the="the Pi" The="The Pi" logs="journalctl -u gus -n 30"
  if [ -n "${GUS_LOCAL:-}" ]; then name="Gus"; the="Gus"; The="Gus"; logs="gus logs"; fi

  local busy
  busy=$(curl -s --max-time 10 "$url/api/run" | python3 -c "import json,sys; print(','.join(json.load(sys.stdin).get('running', {})))" 2>/dev/null || true)
  [ -z "$busy" ] || { echo "Not restarting: $busy is running. Try again when it's finished."; exit 1; }
  [ -z "$(git status --porcelain)" ] || { echo "$The has local changes:"; git status --short; exit 1; }

  local previous branch label
  previous=$(git rev-parse --short HEAD)
  branch=$(git symbolic-ref -q --short HEAD || true)
  if [ -n "$target" ]; then
    label=$target
    fetch() { git pull -q --ff-only && [ "$(git rev-parse --short HEAD)" = "$target" ]; }
    back() { git reset -q --hard "$previous"; }
  else
    git fetch -q --tags origin
    label=$(git tag --list 'v[0-9]*' --sort=-v:refname | head -1)
    [ -n "$label" ] || { echo "There is no release to update to yet."; exit 1; }
    target=$(git rev-parse --short "$label^{commit}")
    [ "$previous" != "$target" ] || { echo "$name is up to date ($label)."; exit 0; }
    git merge-base --is-ancestor HEAD "$label" || { echo "This copy of Gus is ahead of the latest release ($label), so there is nothing to update."; exit 0; }
    fetch() { git checkout -q --detach "$label"; }
    back() { if [ -n "$branch" ]; then git checkout -q "$branch"; else git checkout -q --detach "$previous"; fi; }
  fi

  healthy() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url/")
    [ "$code" != 401 ] || return 0  # answering, behind your cockpit token
    [ "$code" = 200 ] && curl -s --max-time 5 "$url/api/state" | python3 -c "import json,sys; json.load(sys.stdin)['jobs']" 2>/dev/null
  }

  restart() {
    if [ -n "${GUS_LOCAL:-}" ]; then
      bash deploy/gus.sh restart >/dev/null 2>&1 || true
    else
      pkill -KILL -f "radar.service" || true   # systemd restarts it; no sudo needed
    fi
    for _ in $(seq 1 12); do
      sleep 3
      healthy && return 0
    done
    return 1
  }

  if fetch && python3 -m compileall -q radar && restart; then
    echo "$name is running $label"
    exit 0
  fi

  echo "$label didn't work. Going back to $previous."
  back
  if restart; then
    echo "Rolled back: $the is running $previous. Check what went wrong with: $logs"
  else
    echo "Rolled back to $previous, but the cockpit still isn't responding. Check: $logs"
  fi
  exit 1
}

main "$@"
