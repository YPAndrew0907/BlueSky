#!/usr/bin/env bash
set -euo pipefail

ROOT="/Volumes/T9/BlueSky"
START_SCRIPT="$ROOT/scripts/collector_watchdog_start.sh"
CONTROL_DIR="$ROOT/data_v2_full/control"
PID_FILE="$CONTROL_DIR/collector_daemon.pid"
WATCHDOG_CRON_LOG="$ROOT/data_v2_full/logs/launchd/collector-watchdog-cron.log"
CRON_TAG_START="# BEGIN_BLUESKY_COLLECTOR_WATCHDOG"
CRON_TAG_END="# END_BLUESKY_COLLECTOR_WATCHDOG"

mkdir -p "$CONTROL_DIR"

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

find_daemon_pid() {
  local daemon="$ROOT/scripts/collector_daemon.sh"
  local pid cmd
  while IFS= read -r line; do
    pid="${line%% *}"
    cmd="${line#* }"
    case "$cmd" in
      "$daemon"*) echo "$pid $cmd" ;;
      "bash $daemon"*) echo "$pid $cmd" ;;
      "/bin/bash $daemon"*) echo "$pid $cmd" ;;
      "sh $daemon"*) echo "$pid $cmd" ;;
    esac
  done < <(ps -axo 'pid=,command=')
}

install_cron() {
  local tmp
  tmp="$(mktemp)"
  local existing
  existing="$(crontab -l 2>/dev/null || true)"
  {
    printf '%s\n' "$existing" | awk -v s="$CRON_TAG_START" -v e="$CRON_TAG_END" '
      $0==s {skip=1; next}
      $0==e {skip=0; next}
      !skip {print}
    '
    echo "$CRON_TAG_START"
    echo "@reboot /bin/bash $START_SCRIPT >> $WATCHDOG_CRON_LOG 2>&1"
    echo "* * * * * /bin/bash $START_SCRIPT >> $WATCHDOG_CRON_LOG 2>&1"
    echo "$CRON_TAG_END"
  } > "$tmp"
  crontab "$tmp"
  rm -f "$tmp"
  echo "watchdog cron installed"
}

uninstall_cron() {
  local tmp
  tmp="$(mktemp)"
  (crontab -l 2>/dev/null || true) | awk -v s="$CRON_TAG_START" -v e="$CRON_TAG_END" '
    $0==s {skip=1; next}
    $0==e {skip=0; next}
    !skip {print}
  ' > "$tmp"
  crontab "$tmp"
  rm -f "$tmp"
  echo "watchdog cron removed"
}

start_now() {
  "$START_SCRIPT"
  echo "watchdog start attempted"
}

stop_now() {
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]]; then
      kill "$pid" >/dev/null 2>&1 || true
      sleep 1
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    rm -f "$PID_FILE"
  fi
  # Also stop any stray daemon process.
  pkill -f "$ROOT/scripts/collector_daemon.sh" >/dev/null 2>&1 || true
  pkill -f "$ROOT/scripts/collector_study_daemon.sh" >/dev/null 2>&1 || true
  pkill -f "bsky_collector_v2 micro-snapshot-study" >/dev/null 2>&1 || true
  stop_state_writer
  echo "daemon stopped"
}

status_now() {
  echo "=== daemon process ==="
  local found
  found="$(find_daemon_pid || true)"
  if [[ -n "$found" ]]; then
    echo "$found"
  else
    echo "not running"
  fi
  echo
  echo "=== cron entries ==="
  (crontab -l 2>/dev/null || true) | awk -v s="$CRON_TAG_START" -v e="$CRON_TAG_END" '
    $0==s {show=1; print; next}
    $0==e {print; show=0; next}
    show {print}
  '
  return 0
}

case "${1:-}" in
  install)
    install_cron
    start_now
    ;;
  uninstall)
    uninstall_cron
    ;;
  start)
    start_now
    ;;
  stop)
    stop_now
    ;;
  restart)
    stop_now
    start_now
    ;;
  status)
    status_now
    ;;
  *)
    cat <<USAGE
Usage: $0 {install|uninstall|start|stop|restart|status}
USAGE
    exit 2
    ;;
esac
