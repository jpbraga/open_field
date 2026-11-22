#!/bin/zsh
# Install (or reinstall) the openField daemon as a launchd user agent: starts at login, restarts if it dies.
set -euo pipefail
ROOT=${0:A:h:h}
LABEL=com.openfield.daemon
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
DATA=$HOME/.local/share/openfield
mkdir -p "$DATA" "$HOME/Library/LaunchAgents"
[[ -x $ROOT/.venv/bin/openfield ]] || (cd "$ROOT" && uv sync)
"$ROOT/.venv/bin/openfield" --config "$ROOT/config" check

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/openfield</string><string>--config</string><string>$ROOT/config</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$DATA/daemon.log</string>
  <key>StandardErrorPath</key><string>$DATA/daemon.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; log: $DATA/daemon.log"
