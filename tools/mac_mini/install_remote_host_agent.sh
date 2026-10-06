#!/usr/bin/env bash

set -euo pipefail

LABEL="com.github.yep.virtualos.remotehost"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
WRAPPER_PATH="${REPO_DIR}/tools/mac_mini/start_remote_host_at_login.sh"
ENV_FILE="${HOME}/.config/virtualos/remote_host.env"
LOG_OUT="${HOME}/Library/Logs/virtualos-remote-host.log"
LOG_ERR="${HOME}/Library/Logs/virtualos-remote-host.err.log"

mkdir -p "$PLIST_DIR"
mkdir -p "$(dirname "$ENV_FILE")"
mkdir -p "$(dirname "$LOG_OUT")"

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
  <key>LimitLoadToSessionType</key>
  <array>
    <string>Aqua</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>StandardOutPath</key>
  <string>${LOG_OUT}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_ERR}</string>
</dict>
</plist>
EOF

chmod 644 "$PLIST_PATH"
chmod +x "$WRAPPER_PATH"

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "${ENV_FILE}.example" <<'EOF'
VOS_REMOTE_HOST=127.0.0.1
VOS_REMOTE_PORT=8899
# Required when VOS_REMOTE_HOST is not loopback:
# VOS_AUTH_TOKEN=replace-me
# VOS_WATCH_REPOS=/Users/ai/Documents/GitHub/virtualOS,/Users/ai/Documents/GitHub/ai-share
EOF
fi

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

echo "Installed LaunchAgent: $PLIST_PATH"
echo "Wrapper script: $WRAPPER_PATH"
echo "Logs:"
echo "  $LOG_OUT"
echo "  $LOG_ERR"
echo
echo "Optional env file:"
echo "  $ENV_FILE"
echo
echo "If you want custom repo polling or auth, create it from:"
echo "  ${ENV_FILE}.example"
