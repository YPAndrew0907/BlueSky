#!/usr/bin/env bash
set -euo pipefail

ROOT="/Volumes/T9/BlueSky"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/com.bluesky.collector-daemon.plist"
LABEL="com.bluesky.collector-daemon"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"
DAEMON_SCRIPT="$ROOT/scripts/collector_daemon.sh"
OUT_LOG="$ROOT/data_v2_full/logs/launchd/collector-daemon.stdout.log"
ERR_LOG="$ROOT/data_v2_full/logs/launchd/collector-daemon.stderr.log"

mkdir -p "$AGENT_DIR" "$ROOT/data_v2_full/logs/launchd"

write_plist() {
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DAEMON_SCRIPT</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$OUT_LOG</string>
  <key>StandardErrorPath</key>
  <string>$ERR_LOG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
EOF
}

ensure_domain() {
  if launchctl print "$DOMAIN" >/dev/null 2>&1; then
    return 0
  fi
  DOMAIN="user/$UID_NUM"
}

cmd_start() {
  ensure_domain
  write_plist
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  launchctl kickstart -k "$DOMAIN/$LABEL"
  echo "started $LABEL"
}

cmd_stop() {
  ensure_domain
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  echo "stopped $LABEL"
}

cmd_status() {
  ensure_domain
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl print "$DOMAIN/$LABEL" | sed -n '1,120p'
  else
    echo "$LABEL not loaded"
  fi
}

cmd_logs() {
  tail -n 80 "$ROOT/data_v2_full/logs/launchd/collector-daemon.log" 2>/dev/null || true
  tail -n 40 "$OUT_LOG" 2>/dev/null || true
  tail -n 40 "$ERR_LOG" 2>/dev/null || true
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  *)
    cat <<USAGE
Usage: $0 {start|stop|restart|status|logs}
USAGE
    exit 2
    ;;
esac
