#!/usr/bin/env bash
# Install Gus on your own Mac or Linux computer: it starts when you log in, and `gus` controls it.
#   curl -fsSL https://raw.githubusercontent.com/SI-LC/gus/main/deploy/install.sh | bash
#   or, from a copy you already have:  bash deploy/install.sh
# GUS_HOME says where it goes (default ~/Gus). For an always-on Raspberry Pi use deploy/install-pi.sh.
set -euo pipefail

main() {  # read whole before it starts, so a download that stops halfway runs nothing
  local repo="${GUS_REPO:-https://github.com/SI-LC/gus}" port="${GUS_PORT:-8484}" bin="${GUS_BIN:-$HOME/.local/bin}"
  local label=com.gus.cockpit agents="${GUS_AGENTS:-$HOME/Library/LaunchAgents}" units="${GUS_UNITS:-$HOME/.config/systemd/user}"
  local path="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  say() { echo "  $1"; }
  stop() { echo "Gus wasn't installed: $1" >&2; exit 1; }

  echo "Installing Gus"
  command -v git >/dev/null || stop "it needs git. Install git and run this again."
  local py
  py=$(command -v python3) || stop "it needs Python 3.9 or later. Install it from python.org and run this again."
  "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || stop "it needs Python 3.9 or later, and this computer has $("$py" -V 2>&1)."

  # Where: the copy this script is in, unless it was piped in or GUS_HOME says somewhere else
  local here="" src="${BASH_SOURCE[0]:-}"
  if [ -n "$src" ] && [ -f "$(dirname "$src")/../radar/service.py" ]; then here="$(cd "$(dirname "$src")/.." && pwd)"; fi
  local dir="${GUS_HOME:-${here:-$HOME/Gus}}"
  case "$dir" in *" "*) stop "the folder's name can't have a space in it: $dir" ;; esac
  if [ -d "$dir/.git" ] && [ -f "$dir/radar/service.py" ]; then
    say "Using the copy of Gus in $dir"
  elif [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    stop "$dir is already there and isn't Gus. Set GUS_HOME to another folder."
  else
    git clone -q "$repo" "$dir" || stop "couldn't download it from $repo."
    local tag
    tag=$(git -C "$dir" tag --list 'v[0-9]*' --sort=-v:refname | head -1)
    [ -z "$tag" ] || git -C "$dir" checkout -q --detach "$tag"
    say "Gus ${tag:-} is in $dir"
  fi
  dir="$(cd "$dir" && pwd)"
  git -C "$dir" config core.hooksPath .githooks  # so your CV and contacts can't be committed by mistake

  # A computer that sleeps or is switched off misses the daily time: have a new install catch up when it starts
  [ -f "$dir/settings.json" ] || (cd "$dir" && "$py" -c "from radar import settings; settings.save({'catch_up_missed_run': True})")

  mkdir -p "$bin"
  printf '#!/usr/bin/env bash\nGUS_PORT=%s exec bash "%s/deploy/gus.sh" "$@"\n' "$port" "$dir" > "$bin/gus"
  chmod +x "$bin/gus"
  say "The gus command is in $bin"

  local service=""
  if [ "$(uname)" = Darwin ]; then
    mkdir -p "$agents" "$HOME/Library/Logs"
    cat > "$agents/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$py</string><string>-m</string><string>radar.service</string><string>--port</string><string>$port</string></array>
  <key>WorkingDirectory</key><string>$dir</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$path</string><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/gus.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/gus.log</string>
</dict></plist>
PLIST
    service=yes
  elif [ -n "${GUS_NO_START:-}" ] || systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$units"
    cat > "$units/gus.service" <<UNIT
[Unit]
Description=Gus cockpit and scheduler
After=network-online.target

[Service]
WorkingDirectory=$dir
Environment=PATH=$path
Environment=PYTHONUNBUFFERED=1
ExecStart=$py -m radar.service --port $port
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
UNIT
    service=yes
  fi

  if [ -z "$service" ]; then
    say "This computer can't start Gus for you. Start it yourself: cd $dir && python3 -m radar.service"
  elif [ -n "${GUS_NO_START:-}" ]; then
    say "Set up to start when you log in (not started now)"
  else
    if [ "$(uname)" != Darwin ]; then
      systemctl --user daemon-reload
      systemctl --user enable -q gus
    fi
    GUS_PORT="$port" bash "$dir/deploy/gus.sh" restart >/dev/null
    local n
    for n in $(seq 1 20); do
      GUS_PORT="$port" bash "$dir/deploy/gus.sh" status >/dev/null 2>&1 && break
      sleep 1
    done
    if [ "$n" = 20 ]; then
      say "Gus was set up but isn't answering yet. See why with: gus logs"
    else
      say "Gus is running at http://127.0.0.1:$port and starts when you log in"
      [ -n "${GUS_NO_OPEN:-}" ] || GUS_PORT="$port" bash "$dir/deploy/gus.sh" open >/dev/null 2>&1 || true
    fi
    [ "$(uname)" = Darwin ] || say "To keep it running while you're logged out: loginctl enable-linger $USER"
  fi

  PATH="$path:$PATH" command -v claude >/dev/null || say "Scoring roles needs Claude Code (https://claude.com/claude-code) or an API key, which you can add in the cockpit."
  case ":$PATH:" in *":$bin:"*) ;; *) say "$bin isn't on your path yet. Until it is, run $bin/gus" ;; esac
  echo "Done. Next: gus open, then paste your CV. gus on its own lists what else it does."
}

main "$@"
