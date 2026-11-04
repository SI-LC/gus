#!/usr/bin/env bash
# One-off, run once on the Pi after the repo is renamed: sudo bash migrate-to-gus.sh
# Moves ~/pm-radar to ~/gus and swaps the pm-radar service for gus. Takes a backup first, and puts
# everything back by itself if Gus doesn't come up. Delete this script once it has run.
set -euo pipefail
OWNER="${SUDO_USER:?Run this with sudo}"
HOME_DIR="${GUS_HOME:-$(getent passwd "$OWNER" | cut -d: -f6)}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
OLD="$HOME_DIR/pm-radar" NEW="$HOME_DIR/gus"
REPO="${GUS_REPO:-https://github.com/SI-LC/gus.git}"
as_owner() { sudo -u "$OWNER" "$@"; }

[ -d "$OLD" ] || { echo "$OLD isn't there, so there's nothing to move."; exit 1; }
[ ! -e "$NEW" ] || { echo "$NEW already exists. Stopping."; exit 1; }
busy=$(curl -s --max-time 10 http://127.0.0.1:8484/api/run | python3 -c "import json,sys; print(','.join(json.load(sys.stdin)['running']))")
[ -z "$busy" ] || { echo "Not moving: $busy is running. Try again when it's finished."; exit 1; }
[ -z "$(as_owner git -C "$OLD" status --porcelain)" ] || { echo "The Pi has local changes:"; as_owner git -C "$OLD" status --short; exit 1; }

backup="$HOME_DIR/pm-radar-backup-$(date +%Y%m%d-%H%M%S).tgz"
(umask 077; tar czf "$backup" -C "$HOME_DIR" pm-radar)   # private from the start: it holds your keys, profile and data
chown "$OWNER" "$backup"
echo "Backed up to $backup"

previous=$(as_owner git -C "$OLD" rev-parse HEAD)
remote=$(as_owner git -C "$OLD" remote get-url origin)
put_back() {
  echo "That didn't work. Putting everything back."
  systemctl disable --now gus.service 2>/dev/null || true
  rm -f "$UNIT_DIR/gus.service"
  [ -d "$NEW" ] && mv "$NEW" "$OLD"
  as_owner git -C "$OLD" remote set-url origin "$remote"
  as_owner git -C "$OLD" reset -q --hard "$previous"
  systemctl daemon-reload
  systemctl enable --now pm-radar.service
  echo "Back on pm-radar at ${previous:0:7}. Your backup is still at $backup."
  exit 1
}
trap put_back ERR

healthy() {
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8484/)" = 200 ] &&
    curl -s --max-time 5 http://127.0.0.1:8484/api/state | python3 -c "import json,sys; json.load(sys.stdin)['jobs']" 2>/dev/null
}

systemctl disable --now pm-radar.service
mv "$OLD" "$NEW"
as_owner git -C "$NEW" remote set-url origin "$REPO"
as_owner git -C "$NEW" pull -q --ff-only
bash "$NEW/deploy/install-pi.sh"

up=
for _ in $(seq 1 20); do
  sleep 3
  if healthy; then up=1; break; fi
done
[ -n "$up" ] || false   # goes to put_back

trap - ERR
rm -f "$UNIT_DIR/pm-radar.service"
systemctl daemon-reload
echo "Gus is running from $NEW as the gus service. Logs: journalctl -u gus -f"
