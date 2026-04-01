#!/usr/bin/env bash
set -euo pipefail

ROOT="/Volumes/T9/BlueSky"
DAEMON="$ROOT/scripts/collector_daemon.sh"
CONTROL_DIR="$ROOT/data_v2_full/control"
LOG_DIR="$ROOT/data_v2_full/logs/launchd"
PID_FILE="$CONTROL_DIR/collector_daemon.pid"
WATCHDOG_LOG="$LOG_DIR/collector-watchdog.log"

mkdir -p "$CONTROL_DIR" "$LOG_DIR"

log_msg() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$WATCHDOG_LOG"
}

find_daemon_pid() {
  local pid cmd
  while IFS= read -r line; do
    pid="${line%% *}"
    cmd="${line#* }"
    case "$cmd" in
      "$DAEMON"*) echo "$pid"; return 0 ;;
      "bash $DAEMON"*) echo "$pid"; return 0 ;;
      "/bin/bash $DAEMON"*) echo "$pid"; return 0 ;;
      "sh $DAEMON"*) echo "$pid"; return 0 ;;
    esac
  done < <(ps -axo 'pid=,command=')
  return 1
}

pid_alive() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

pid_is_daemon_process() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  local cmd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$cmd" in
    "$DAEMON"*) return 0 ;;
    "bash $DAEMON"*) return 0 ;;
    "/bin/bash $DAEMON"*) return 0 ;;
    "sh $DAEMON"*) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if pid_alive "$existing_pid" && pid_is_daemon_process "$existing_pid"; then
    exit 0
  fi
  rm -f "$PID_FILE"
fi

existing_daemon_pid="$(find_daemon_pid || true)"
if [[ -n "$existing_daemon_pid" ]] && pid_alive "$existing_daemon_pid"; then
  # Another live daemon exists; refresh pid file and exit.
  printf '%s\n' "$existing_daemon_pid" > "$PID_FILE"
  exit 0
fi

nohup "$DAEMON" >> "$LOG_DIR/collector-daemon.stdout.log" 2>> "$LOG_DIR/collector-daemon.stderr.log" &
new_pid=$!
printf '%s\n' "$new_pid" > "$PID_FILE"
log_msg "daemon_started pid=$new_pid"
