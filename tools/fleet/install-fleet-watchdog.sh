#!/usr/bin/env bash
# Install (or reload) the fleet watchdog LaunchAgent on a host.
#
#   tools/fleet/install-fleet-watchdog.sh
#
# The watchdog keeps the live stack converged: when the relay heartbeat goes
# stale it brings the supervisor, a VM, and the stream/control agent back up.
# Idempotent: re-running reloads the agent.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
LABEL="com.virtualos.fleet-watchdog"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$FLEET_HOME/watchdog.log"

mkdir -p "$HOME/Library/LaunchAgents" "$FLEET_HOME"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>KeepAlive</key>
	<true/>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>/bin/bash</string>
		<string>-lc</string>
		<string>exec $FLEET_HOME/tools/fleet/fleet-watchdog.sh</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardErrorPath</key>
	<string>$LOG</string>
	<key>StandardOutPath</key>
	<string>$LOG</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed $LABEL -> $PLIST (log: $LOG)"
launchctl list | grep "$LABEL" || true
