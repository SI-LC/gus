#!/usr/bin/env bash
# The `gus` command on your own computer, put on your path by deploy/install.sh.
#   gus start | stop | restart | status | open | logs | update | connect | uninstall
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${GUS_PORT:-8484}"
URL="http://127.0.0.1:$PORT"
LABEL=com.gus.cockpit
PLIST="${GUS_AGENTS:-$HOME/Library/LaunchAgents}/$LABEL.plist"
UNIT="${GUS_UNITS:-$HOME/.config/systemd/user}/gus.service"
LOG="$HOME/Library/Logs/gus.log"
BIN="${GUS_BIN:-$HOME/.local/bin}"

mac() { [ "$(uname)" = Darwin ]; }
me() { echo "gui/$(id -u)"; }
up() { case "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$URL/" 2>/dev/null)" in 200|401) return 0 ;; *) return 1 ;; esac; }
release() { git -C "$DIR" describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || git -C "$DIR" rev-parse --short HEAD; }
installed() {
  if mac; then [ -f "$PLIST" ]; else [ -f "$UNIT" ]; fi || { echo "Gus isn't set up to run on this computer. Run: bash $DIR/deploy/install.sh"; exit 1; }
}

start() {
  installed
  if mac; then
    launchctl bootstrap "$(me)" "$PLIST" 2>/dev/null || launchctl kickstart "$(me)/$LABEL"
  else
    systemctl --user start gus
  fi
}

stop() {
  if mac; then launchctl bootout "$(me)/$LABEL" 2>/dev/null || true; else systemctl --user stop gus 2>/dev/null || true; fi
}

restart() {
  installed
  if mac; then
    launchctl kickstart -k "$(me)/$LABEL" 2>/dev/null || launchctl bootstrap "$(me)" "$PLIST"
  else
    systemctl --user restart gus
  fi
}

case "${1:-help}" in
  start) start; echo "Gus is starting at $URL" ;;
  stop) stop; echo "Gus has stopped. It won't start again until you run: gus start" ;;
  restart) restart; echo "Gus is restarting at $URL" ;;
  status)
    if up; then echo "Gus $(release) is running at $URL"; else echo "Gus isn't running. Start it with: gus start"; exit 1; fi ;;
  open)
    up || { echo "Gus isn't running. Start it with: gus start"; exit 1; }
    if mac; then open "$URL"; elif command -v xdg-open >/dev/null; then xdg-open "$URL"; else echo "Open $URL in your browser."; fi ;;
  logs)
    if mac; then tail -n 50 -f "$LOG"; else journalctl --user -u gus -n 50 -f; fi ;;
  update)
    GUS_LOCAL=1 GUS_DIR="$DIR" GUS_PORT="$PORT" exec bash "$DIR/deploy/update.sh" ;;  # exec: the update replaces this file too
  connect)
    echo "Claude Code, in a terminal:"
    echo "  claude mcp add --transport http gus $URL/mcp"
    echo
    echo "The Claude desktop app: Settings, Developer, Edit Config, add this and restart the app:"
    python3 -c 'import json, sys; print("  " + json.dumps({"mcpServers": {"gus": {"command": "python3", "args": ["-m", "radar.mcp", "--url", sys.argv[1] + "/mcp"], "env": {"PYTHONPATH": sys.argv[2]}}}}))' "$URL" "$DIR"
    echo
    echo "From another device, make a token under Settings, Claude, in the cockpit." ;;
  uninstall)
    stop
    if mac; then rm -f "$PLIST"; else systemctl --user disable gus >/dev/null 2>&1 || true; rm -f "$UNIT"; fi
    rm -f "$BIN/gus"
    echo "Gus no longer runs on this computer. Your search is still in $DIR: delete that folder to remove it too." ;;
  *)
    cat <<HELP
gus start      start Gus, now and every time you log in
gus stop       stop it until you start it again
gus status     is it running, and which release
gus open       open the cockpit in your browser
gus logs       what Gus has been doing
gus update     move to the latest release; goes back by itself if that doesn't work
gus connect    how to connect Claude to Gus
gus uninstall  stop Gus running here; your search is left where it is
HELP
    ;;
esac
