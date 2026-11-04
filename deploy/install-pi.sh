#!/usr/bin/env bash
# Run once on the Pi, from the repo folder: sudo bash deploy/install-pi.sh
# Starts PM Radar on boot, listening on this machine only, and publishes the cockpit
# to your own Tailscale devices with HTTPS. Nothing is opened on your home network.
set -euo pipefail
OWNER="${SUDO_USER:?Run this with sudo}"
HOME_DIR="$(getent passwd "$OWNER" | cut -d: -f6)"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8484}"

cat > /etc/systemd/system/pm-radar.service <<UNIT
[Unit]
Description=PM Radar cockpit and scheduler
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=$OWNER
WorkingDirectory=$DIR
Environment=HOME=$HOME_DIR
Environment=PATH=$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m radar.service --port $PORT
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
MemoryMax=1400M

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now pm-radar.service
tailscale serve --bg "$PORT"
tailscale serve status
echo "PM Radar is running. Logs: journalctl -u pm-radar -f"
