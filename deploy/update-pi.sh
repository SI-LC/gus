#!/usr/bin/env bash
# Runs on the Pi, sent by deploy.sh: update to a commit, check it works, and go back to the last commit if it doesn't.
#   bash -s <short commit> < deploy/update-pi.sh
set -euo pipefail
cd ~/gus
target=$1

busy=$(curl -s --max-time 10 http://127.0.0.1:8484/api/run | python3 -c "import json,sys; print(','.join(json.load(sys.stdin)['running']))")
[ -z "$busy" ] || { echo "Not restarting: $busy is running. Deploy again when it's finished."; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "The Pi has local changes:"; git status --short; exit 1; }

healthy() {
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8484/)" = 200 ] &&
    curl -s --max-time 5 http://127.0.0.1:8484/api/state | python3 -c "import json,sys; json.load(sys.stdin)['jobs']" 2>/dev/null
}

restart() {
  pkill -KILL -f "radar.service" || true   # systemd restarts it; no sudo needed
  for _ in $(seq 1 12); do
    sleep 3
    healthy && return 0
  done
  return 1
}

previous=$(git rev-parse --short HEAD)
works() {
  git pull -q --ff-only && [ "$(git rev-parse --short HEAD)" = "$target" ] && python3 -m compileall -q radar && restart
}

if works; then
  echo "Pi is running $target"
  exit 0
fi

echo "$target didn't work. Going back to $previous."
git reset -q --hard "$previous"
if restart; then
  echo "Rolled back: the Pi is running $previous. Check what went wrong with: journalctl -u gus -n 30"
else
  echo "Rolled back to $previous, but the cockpit still isn't responding. Check: journalctl -u gus -n 30"
fi
exit 1
