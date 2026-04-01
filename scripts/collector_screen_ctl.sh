#!/usr/bin/env bash
set -euo pipefail

ROOT="/Volumes/T9/BlueSky"
SESSION_NAME="bsky_collector_daemon"
DAEMON_SCRIPT="$ROOT/scripts/collector_daemon.sh"
LOG_FILE="$ROOT/data_v2_full/logs/launchd/collector-daemon.log"
WRAPPER_LOG="$ROOT/data_v2_full/logs/launchd/collector-daemon-wrapper.log"
CONTROL_DIR="$ROOT/data_v2_full/control"

stop_state_writer() {
  local pid_file="$CONTROL_DIR/state_writer.pid"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]]; then
      kill "$pid" >/dev/null 2>&1 || true
      sleep 1
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  fi
  pkill -f "bsky_collector_v2 state-writer" >/dev/null 2>&1 || true
  rm -f "$CONTROL_DIR/state_writer.pid" "$CONTROL_DIR/state_writer.socket" "$CONTROL_DIR/state_writer.logpath"
}

screen_session_exists() {
  (screen -ls 2>/dev/null || true) | rg -q "[[:digit:]]+\\.${SESSION_NAME}[[:space:]]"
}

start_session() {
  if screen_session_exists; then
    echo "screen session already running: $SESSION_NAME"
    return 0
  fi
  screen -dmS "$SESSION_NAME" /bin/bash -lc "cd '$ROOT' && while true; do '$DAEMON_SCRIPT'; rc=\$?; printf '%s daemon_wrapper_restart rc=%s\\n' \"\$(date -u '+%Y-%m-%dT%H:%M:%SZ')\" \"\$rc\" >> '$WRAPPER_LOG'; sleep 5; done"
  sleep 1
  if screen_session_exists; then
    echo "started screen session: $SESSION_NAME"
  else
    echo "failed to start screen session: $SESSION_NAME" >&2
    exit 1
  fi
}

stop_session() {
  if screen_session_exists; then
    screen -S "$SESSION_NAME" -X quit || true
    sleep 1
  fi
  pkill -f "$DAEMON_SCRIPT" >/dev/null 2>&1 || true
  pkill -f "bsky_collector_v2 micro-snapshot-study" >/dev/null 2>&1 || true
  stop_state_writer
  echo "stopped screen session: $SESSION_NAME"
}

status_session() {
  echo "=== screen ==="
  if screen_session_exists; then
    (screen -ls 2>/dev/null || true) | rg "$SESSION_NAME"
  else
    echo "not running"
  fi
  echo
  echo "=== daemon pid file ==="
  cat "$ROOT/data_v2_full/control/collector_daemon.pid" 2>/dev/null || echo "(missing)"
  echo
  echo "=== collector processes ==="
  ps -axo 'pid=,ppid=,stat=,etime=,command=' | rg -N "collector_daemon\\.sh|collector_study_daemon\\.sh|bsky_collector_v2 (state-writer|micro-snapshot-study|snapshot-panel|wide-sweep|hydrate-authors|index-feed-generators|refresh-discovery|build-panel)" || true
}

logs_session() {
  tail -n 80 "$WRAPPER_LOG" 2>/dev/null || true
  tail -n 120 "$LOG_FILE" 2>/dev/null || true
}

attach_session() {
  if ! screen_session_exists; then
    echo "screen session is not running: $SESSION_NAME" >&2
    exit 1
  fi
  exec screen -r "$SESSION_NAME"
}

case "${1:-}" in
  start)
    start_session
    ;;
  stop)
    stop_session
    ;;
  restart)
    stop_session
    start_session
    ;;
  status)
    status_session
    ;;
  logs)
    logs_session
    ;;
  attach)
    attach_session
    ;;
  *)
    cat <<USAGE
Usage: $0 {start|stop|restart|status|logs|attach}
USAGE
    exit 2
    ;;
esac
