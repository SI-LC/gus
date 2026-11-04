#!/bin/zsh
# Install the 10-minute background scan (macOS launchd). Undo: launchctl unload ~/Library/LaunchAgents/com.pmradar.scan.plist
cd "${0:A:h}"
git config core.hooksPath .githooks  # code-only guard
sed "s#__HOME__#$HOME#g" com.pmradar.scan.plist > ~/Library/LaunchAgents/com.pmradar.scan.plist
launchctl unload ~/Library/LaunchAgents/com.pmradar.scan.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.pmradar.scan.plist && echo "PM Radar scanning every 10 minutes."
