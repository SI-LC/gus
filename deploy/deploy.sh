#!/usr/bin/env bash
# The only way to deploy: scan what's merged on main, update the Pi, check it's serving that commit, publish a release.
#   bash deploy/deploy.sh [patch|minor|major]   (default patch)
set -euo pipefail
cd "$(dirname "$0")/.."
PI="${PM_RADAR_PI:-sipi}"
bump="${1:-patch}"
case "$bump" in patch|minor|major) ;; *) echo "Usage: bash deploy/deploy.sh [patch|minor|major]"; exit 1 ;; esac
[ -z "$(git status --porcelain)" ] || { echo "Commit your changes first:"; git status --short; exit 1; }
[ "$(git branch --show-current)" = main ] || { echo "Deploy from main."; exit 1; }

git pull -q --ff-only origin main
git fetch -q --tags origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "main has commits that aren't on GitHub. Put them on a branch and open a pull request."; exit 1; }

bash deploy/scan.sh
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

# The latest release is what's on the Pi. A redeploy of the same commit keeps its version.
current=$(git tag --points-at HEAD --list 'v[0-9]*' | head -1)
if [ -n "$current" ]; then
  echo "Already released as $current"
else
  last=$(git tag --list 'v[0-9]*' --sort=-v:refname | head -1)
  if [ -z "$last" ]; then
    next=v0.1.0
  else
    IFS=. read -r major minor patch <<< "${last#v}"
    case "$bump" in
      major) next="v$((major + 1)).0.0" ;;
      minor) next="v$major.$((minor + 1)).0" ;;
      patch) next="v$major.$minor.$((patch + 1))" ;;
    esac
  fi
  gh release create "$next" --target "$(git rev-parse HEAD)" --title "$next" --generate-notes >/dev/null
  git fetch -q --tags origin
  echo "Released $next"
fi
