#!/usr/bin/env bash

set -euo pipefail

LABEL="com.github.yep.virtualos.start"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
SCRIPT_DIR="${HOME}/Library/Application Support/virtualOS/launch-agent"
WRAPPER_PATH="${SCRIPT_DIR}/start_virtualos_at_login.sh"
LOG_OUT="${HOME}/Library/Logs/virtualos-startup.log"
LOG_ERR="${HOME}/Library/Logs/virtualos-startup.err.log"

mkdir -p "$PLIST_DIR"
mkdir -p "$SCRIPT_DIR"
mkdir -p "$(dirname "$LOG_OUT")"
cp "$REPO_DIR/tools/mac_mini/start_virtualos_at_login.sh" "$WRAPPER_PATH"
cp "$REPO_DIR/start.sh" "$SCRIPT_DIR/start.sh"
chmod +x "$WRAPPER_PATH" "$SCRIPT_DIR/start.sh"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${WRAPPER_PATH}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>LimitLoadToSessionType</key>
  <array>
    <string>Aqua</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${SCRIPT_DIR}</string>
  <key>StandardOutPath</key>
  <string>${LOG_OUT}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_ERR}</string>
</dict>
</plist>
EOF

chmod 644 "$PLIST_PATH"
launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

echo "Installed LaunchAgent: $PLIST_PATH"
echo "Wrapper script: $WRAPPER_PATH"
echo "Logs:"
echo "  $LOG_OUT"
echo "  $LOG_ERR"
echo "Guest auto-login uses VOS_VM_PASSWORD from ~/.config/virtualos/start.env"
