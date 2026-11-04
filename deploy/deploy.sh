#!/usr/bin/env bash
# The only way to deploy: scan, push to GitHub, update the Pi, check it's serving that commit.
#   bash deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PI="${PM_RADAR_PI:-sipi}"
[ -z "$(git status --porcelain)" ] || { echo "Commit your changes first:"; git status --short; exit 1; }
[ "$(git branch --show-current)" = main ] || { echo "Deploy from main."; exit 1; }

bash deploy/scan.sh
git push -q origin main   # also runs the scan, through the pre-push hook
sha=$(git rev-parse --short HEAD)
echo "On GitHub: $sha"

ssh -o BatchMode=yes "$PI" bash -s "$sha" <<'REMOTE'
set -euo pipefail
cd ~/pm-radar
busy=$(curl -s --max-time 10 http://127.0.0.1:8484/api/run | python3 -c "import json,sys; print(','.join(json.load(sys.stdin)['running']))")
[ -z "$busy" ] || { echo "Not restarting: $busy is running. Deploy again when it's finished."; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "The Pi has local changes:"; git status --short; exit 1; }
git pull -q --ff-only
[ "$(git rev-parse --short HEAD)" = "$1" ] || { echo "The Pi didn't reach $1."; exit 1; }
python3 -m compileall -q radar
pkill -KILL -f "radar.service" || true   # systemd restarts it; no sudo needed
for i in $(seq 1 12); do
  sleep 3
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8484/)" = 200 ] && { echo "Pi is running $1"; exit 0; }
done
echo "The cockpit didn't come back. Check: journalctl -u pm-radar -n 30"; exit 1
REMOTE
