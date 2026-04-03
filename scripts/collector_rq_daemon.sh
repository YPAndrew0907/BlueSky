#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
OUT_BASE="${OUT_BASE:-$ROOT/data_v2_full}"
ENV_PATH="${ENV_PATH:-$ROOT/auth.env}"
PYTHON_BIN="${PYTHON_BIN:-}"
SOCKET_PATH="${SOCKET_PATH:-${BSKY_STATE_WRITER_SOCKET:-/tmp/bsky_state_writer_prod.sock}}"
DEFAULT_STUDY_ID="${DEFAULT_STUDY_ID:-micro10_full_live_20260319}"
STUDY_ID="${STUDY_ID:-$DEFAULT_STUDY_ID}"

resolve_python_bin() {
  local -a candidates=()
  # Prefer repo-local interpreters over bare shell aliases like "python3" on Windows.
  if [[ -n "$PYTHON_BIN" && "$PYTHON_BIN" == *[\\/]* ]]; then
    candidates+=("$PYTHON_BIN")
  fi
  candidates+=("$ROOT/.venv/bin/python" "$ROOT/.venv-win/Scripts/python.exe")
  if [[ -n "$PYTHON_BIN" && "$PYTHON_BIN" != *[\\/]* ]]; then
    candidates+=("$PYTHON_BIN")
  fi
  if [[ -n "${PYTHON_BIN_FALLBACK:-}" ]]; then
    candidates+=("$PYTHON_BIN_FALLBACK")
  fi
  candidates+=(python3 python)

  local candidate
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" ]] || continue
    if "$candidate" --version >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  printf 'collector_rq_daemon.sh could not resolve a working python interpreter\n' >&2
  exit 2
}

PYTHON_BIN="$(resolve_python_bin)"

CONTROL_DIR="$OUT_BASE/control"
LOG_DIR="$OUT_BASE/logs/manual_runs"
mkdir -p "$CONTROL_DIR" "$LOG_DIR"

STATE_WRITER_STARTED=0
STATE_WRITER_PID=""

usage() {
  cat <<'EOF'
Usage:
  ./scripts/collector_rq_daemon.sh fixed-panel
  ./scripts/collector_rq_daemon.sh realtime-backfill
  ./scripts/collector_rq_daemon.sh history-backfill
  ./scripts/collector_rq_daemon.sh full-stack

Profiles:
  fixed-panel        Paper-grade 10-minute fixed-panel collection via collector_daemon.sh.
  realtime-backfill  Public-only realtime seed + interactions + RQ1 factor backfill loop.
  history-backfill   One-shot seed-post-registry + interactions + RQ1 factor history backfill.
  full-stack         Start fixed-panel daemon, wait for its state-writer, then start realtime backfill.

Important:
  - data_v2_full remains the canonical data root.
  - full-stack shares a single BSKY_STATE_WRITER_SOCKET across both daemons.
  - SOCKET_PATH accepts either a unix path or tcp://HOST:PORT.
  - public-only realtime backfill is not equivalent to the auth+unauth fixed-panel study.
EOF
}

log_msg() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

is_pid_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

stop_pid() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  if is_pid_alive "$pid"; then
    kill "$pid" >/dev/null 2>&1 || true
    sleep 1
    if is_pid_alive "$pid"; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  fi
}

state_writer_target_kind() {
  case "$SOCKET_PATH" in
    tcp://*) printf '%s\n' "tcp" ;;
    unix://*|*/*|*\\*) printf '%s\n' "unix" ;;
    *:*) printf '%s\n' "tcp" ;;
    *) printf '%s\n' "unix" ;;
  esac
}

state_writer_socket_path() {
  case "$SOCKET_PATH" in
    unix://*) printf '%s\n' "${SOCKET_PATH#unix://}" ;;
    *) printf '%s\n' "$SOCKET_PATH" ;;
  esac
}

state_writer_tcp_target() {
  case "$SOCKET_PATH" in
    tcp://*) printf '%s\n' "${SOCKET_PATH#tcp://}" ;;
    *) printf '%s\n' "$SOCKET_PATH" ;;
  esac
}

remove_state_writer_target_artifact() {
  if [[ "$(state_writer_target_kind)" == "unix" ]]; then
    rm -f "$(state_writer_socket_path)"
  fi
}

state_writer_responding() {
  "$PYTHON_BIN" - "$SOCKET_PATH" <<'PY' >/dev/null 2>&1
from __future__ import annotations

import json
import socket
import sys
from urllib.parse import urlparse

target = sys.argv[1].strip()
payload = (json.dumps({
    "method": "ping",
    "args": [],
    "kwargs": {},
}) + "\n").encode("utf-8")

def target_kind(raw: str) -> tuple[str, str]:
    if raw.startswith("tcp://"):
        return ("tcp", raw.removeprefix("tcp://"))
    if raw.startswith("unix://"):
        return ("unix", raw.removeprefix("unix://"))
    if ("/" in raw) or ("\\" in raw):
        return ("unix", raw)
    parsed = urlparse("tcp://" + raw)
    if parsed.hostname and parsed.port is not None:
        return ("tcp", raw)
    return ("unix", raw)

kind, value = target_kind(target)
if kind == "unix":
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2.0)
        conn.connect(value)
        conn.sendall(payload)
        data = conn.recv(65536)
else:
    parsed = urlparse("tcp://" + value)
    if not parsed.hostname or parsed.port is None:
        raise SystemExit(1)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=2.0) as conn:
        conn.sendall(payload)
        data = conn.recv(65536)

if not data:
    raise SystemExit(1)

line = data.split(b"\n", 1)[0]
resp = json.loads(line.decode("utf-8"))
if not isinstance(resp, dict) or not bool(resp.get("ok")):
    raise SystemExit(1)
PY
}

find_recent_windows_pid_by_pattern() {
  local pattern="$1"
  local lookback_s="${2:-30}"
  [[ "${OS:-}" == "Windows_NT" ]] || return 1
  command -v powershell.exe >/dev/null 2>&1 || return 1
  local recent_pid
  recent_pid="$(
    JOB_PATTERN="$pattern" JOB_LOOKBACK_S="$lookback_s" powershell.exe -NoProfile -Command '$pattern = $env:JOB_PATTERN; $cutoff = (Get-Date).AddSeconds(-[int]$env:JOB_LOOKBACK_S); $proc = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ("*" + $pattern + "*") -and $_.CreationDate -ge $cutoff } | Sort-Object CreationDate -Descending | Select-Object -First 1 -ExpandProperty ProcessId; if ($proc) { Write-Output $proc }' 2>/dev/null | tr -d '\r' | head -n 1
  )"
  [[ -n "$recent_pid" ]] || return 1
  printf '%s\n' "$recent_pid"
}

start_state_writer_if_needed() {
  local pid_file="$CONTROL_DIR/state_writer.pid"
  if [[ -f "$pid_file" ]]; then
    local existing_pid
    existing_pid="$(cat "$pid_file" 2>/dev/null || true)"
    if is_pid_alive "$existing_pid" && state_writer_responding; then
      return 0
    fi
    if state_writer_responding; then
      rm -f "$CONTROL_DIR/state_writer.pid" \
        "$CONTROL_DIR/state_writer.socket" \
        "$CONTROL_DIR/state_writer.logpath"
      printf '%s\n' "$SOCKET_PATH" > "$CONTROL_DIR/state_writer.socket"
      log_msg "state-writer metadata stale pid=$existing_pid target=$SOCKET_PATH reusing external listener"
      return 0
    fi
  fi
  if state_writer_responding; then
    printf '%s\n' "$SOCKET_PATH" > "$CONTROL_DIR/state_writer.socket"
    log_msg "state-writer already responding target=$SOCKET_PATH reusing external listener"
    return 0
  fi
  remove_state_writer_target_artifact
  local log_file="$LOG_DIR/state-writer_rq_$(date -u '+%Y%m%dT%H%M%SZ').log"
  local target_kind
  target_kind="$(state_writer_target_kind)"
  if [[ "$target_kind" == "tcp" ]]; then
    "$PYTHON_BIN" -m bsky_collector_v2 state-writer \
      --out-base "$OUT_BASE" \
      --tcp "$(state_writer_tcp_target)" \
      > "$log_file" 2>&1 &
  else
    "$PYTHON_BIN" -m bsky_collector_v2 state-writer \
      --out-base "$OUT_BASE" \
      --socket-path "$(state_writer_socket_path)" \
      > "$log_file" 2>&1 &
  fi
  STATE_WRITER_PID="$!"
  STATE_WRITER_STARTED=1
  printf '%s\n' "$STATE_WRITER_PID" > "$pid_file"
  printf '%s\n' "$SOCKET_PATH" > "$CONTROL_DIR/state_writer.socket"
  printf '%s\n' "$log_file" > "$CONTROL_DIR/state_writer.logpath"
  local attempt
  for attempt in $(seq 1 20); do
    if state_writer_responding; then
      local active_pid
      active_pid="$(find_recent_windows_pid_by_pattern '-m bsky_collector_v2 state-writer' 30 2>/dev/null || true)"
      if [[ -z "$active_pid" ]]; then
        active_pid="$STATE_WRITER_PID"
      fi
      if [[ -n "$active_pid" ]]; then
        STATE_WRITER_PID="$active_pid"
        printf '%s\n' "$STATE_WRITER_PID" > "$pid_file"
      fi
      log_msg "state-writer started pid=$STATE_WRITER_PID target=$SOCKET_PATH kind=$target_kind"
      return 0
    fi
    sleep 0.5
  done
  log_msg "state-writer failed to become ready target=$SOCKET_PATH kind=$target_kind log=$log_file"
  return 1
}

cleanup_state_writer() {
  if [[ "$STATE_WRITER_STARTED" == "1" ]]; then
    stop_pid "$STATE_WRITER_PID"
    remove_state_writer_target_artifact
    rm -f "$CONTROL_DIR/state_writer.pid" \
      "$CONTROL_DIR/state_writer.socket" \
      "$CONTROL_DIR/state_writer.logpath"
  fi
}

run_history_backfill() {
  trap cleanup_state_writer EXIT
  start_state_writer_if_needed
  export BSKY_STATE_WRITER_SOCKET="$SOCKET_PATH"

  local include_hourly="${INCLUDE_HOURLY:-1}"
  local include_wide="${INCLUDE_WIDE:-1}"
  local include_micro5="${INCLUDE_MICRO5:-1}"
  local include_posts_first_seen="${INCLUDE_POSTS_FIRST_SEEN:-1}"
  local run_seed="${RUN_SEED_POST_REGISTRY:-1}"
  local run_interactions="${RUN_BACKFILL_INTERACTIONS:-1}"
  local run_rq1="${RUN_BACKFILL_RQ1_FACTORS:-1}"
  local history_seen_before="${HISTORY_SEEN_BEFORE_UTC:-}"
  local history_seen_after="${HISTORY_SEEN_AFTER_UTC:-}"
  local history_max_posts="${HISTORY_MAX_POSTS:-200000}"
  local history_batch_size="${HISTORY_BATCH_SIZE:-25}"
  local history_max_items="${HISTORY_MAX_ITEMS_PER_ENDPOINT:-0}"
  local history_thread_depth="${HISTORY_MAX_THREAD_DEPTH:-1000}"
  local history_thread_parent_height="${HISTORY_MAX_THREAD_PARENT_HEIGHT:-1000}"

  if [[ "$run_seed" == "1" ]]; then
    local -a seed_cmd=(
      "$PYTHON_BIN" -m bsky_collector_v2 seed-post-registry
      --out-base "$OUT_BASE"
      "$( [[ "$include_hourly" == "1" ]] && echo --include-hourly || echo --no-include-hourly )"
      "$( [[ "$include_wide" == "1" ]] && echo --include-wide || echo --no-include-wide )"
      "$( [[ "$include_micro5" == "1" ]] && echo --include-micro5 || echo --no-include-micro5 )"
      "$( [[ "$include_posts_first_seen" == "1" ]] && echo --include-posts-first-seen || echo --no-include-posts-first-seen )"
    )
    log_msg "history-backfill running: ${seed_cmd[*]}"
    "${seed_cmd[@]}"
  fi

  if [[ "$run_interactions" == "1" ]]; then
    local -a interaction_cmd=(
      "$PYTHON_BIN" -m bsky_collector_v2 backfill-interactions
      --out-base "$OUT_BASE"
      --max-posts "$history_max_posts"
      --batch-size "$history_batch_size"
      --max-items-per-endpoint "$history_max_items"
    )
    if [[ -n "$history_seen_before" ]]; then
      interaction_cmd+=(--seen-before-utc "$history_seen_before")
    fi
    if [[ -n "$history_seen_after" ]]; then
      interaction_cmd+=(--seen-after-utc "$history_seen_after")
    fi
    log_msg "history-backfill running: ${interaction_cmd[*]}"
    "${interaction_cmd[@]}"
  fi

  if [[ "$run_rq1" == "1" ]]; then
    local -a rq1_cmd=(
      "$PYTHON_BIN" -m bsky_collector_v2 backfill-rq1-factors
      --out-base "$OUT_BASE"
      --max-posts "$history_max_posts"
      --batch-size "$history_batch_size"
      --max-items-per-endpoint "$history_max_items"
      --max-thread-depth "$history_thread_depth"
      --max-thread-parent-height "$history_thread_parent_height"
    )
    if [[ -n "$history_seen_before" ]]; then
      rq1_cmd+=(--seen-before-utc "$history_seen_before")
    fi
    if [[ -n "$history_seen_after" ]]; then
      rq1_cmd+=(--seen-after-utc "$history_seen_after")
    fi
    log_msg "history-backfill running: ${rq1_cmd[*]}"
    "${rq1_cmd[@]}"
  fi
}

run_realtime_backfill() {
  trap cleanup_state_writer EXIT
  start_state_writer_if_needed
  export BSKY_STATE_WRITER_SOCKET="$SOCKET_PATH"

  ROOT="$ROOT" \
  OUT_BASE="$OUT_BASE" \
  PYTHON_BIN="$PYTHON_BIN" \
  LOG_LEVEL="${LOG_LEVEL:-info}" \
  LOOP_SLEEP_S="${LOOP_SLEEP_S:-30}" \
  INTERVAL_PUBLIC_OMNIBUS_S="${INTERVAL_PUBLIC_OMNIBUS_S:-300}" \
  RUN_ONCE="${RUN_ONCE:-0}" \
  RESUME="${RESUME:-1}" \
  DRY_RUN="${DRY_RUN:-0}" \
  SEED_REGISTRY="${SEED_REGISTRY:-1}" \
  INCLUDE_POSTS_FIRST_SEEN="${INCLUDE_POSTS_FIRST_SEEN:-1}" \
  ENQUEUE_INTERACTIONS_FROM_SEED="${ENQUEUE_INTERACTIONS_FROM_SEED:-1}" \
  ENQUEUE_RQ1_FACTORS_FROM_SEED="${ENQUEUE_RQ1_FACTORS_FROM_SEED:-1}" \
  RUN_INDEX_FEED_GENERATORS="${RUN_INDEX_FEED_GENERATORS:-0}" \
  RUN_REFRESH_DISCOVERY="${RUN_REFRESH_DISCOVERY:-0}" \
  RUN_BUILD_PANEL="${RUN_BUILD_PANEL:-0}" \
  RUN_SNAPSHOT_PANEL="${RUN_SNAPSHOT_PANEL:-0}" \
  RUN_WIDE_SWEEP="${RUN_WIDE_SWEEP:-0}" \
  RUN_HYDRATE_AUTHORS="${RUN_HYDRATE_AUTHORS:-0}" \
  RUN_HYDRATE_FEED_GENERATORS="${RUN_HYDRATE_FEED_GENERATORS:-0}" \
  RUN_BACKFILL_INTERACTIONS="${RUN_BACKFILL_INTERACTIONS:-1}" \
  RUN_BACKFILL_RQ1_FACTORS="${RUN_BACKFILL_RQ1_FACTORS:-1}" \
  RUN_MICRO_STUDIES="${RUN_MICRO_STUDIES:-0}" \
  MAX_POSTS_INTERACTIONS="${MAX_POSTS_INTERACTIONS:-200000}" \
  MAX_POSTS_RQ1="${MAX_POSTS_RQ1:-200000}" \
  BATCH_SIZE_INTERACTIONS="${BATCH_SIZE_INTERACTIONS:-25}" \
  BATCH_SIZE_RQ1="${BATCH_SIZE_RQ1:-25}" \
  MAX_ITEMS_PER_ENDPOINT_INTERACTIONS="${MAX_ITEMS_PER_ENDPOINT_INTERACTIONS:-0}" \
  MAX_ITEMS_PER_ENDPOINT_RQ1="${MAX_ITEMS_PER_ENDPOINT_RQ1:-0}" \
  MAX_THREAD_DEPTH="${MAX_THREAD_DEPTH:-1000}" \
  MAX_THREAD_PARENT_HEIGHT="${MAX_THREAD_PARENT_HEIGHT:-1000}" \
  SEEN_AFTER_UTC="${SEEN_AFTER_UTC:-}" \
  SEEN_BEFORE_UTC="${SEEN_BEFORE_UTC:-}" \
  "$ROOT/scripts/collector_public_omnivore_daemon.sh"
}

run_fixed_panel() {
  ROOT="$ROOT" \
  OUT_BASE="$OUT_BASE" \
  ENV_PATH="$ENV_PATH" \
  PYTHON_BIN="$PYTHON_BIN" \
  SOCKET_PATH="$SOCKET_PATH" \
  DEFAULT_STUDY_ID="$DEFAULT_STUDY_ID" \
  STUDY_ID="$STUDY_ID" \
  "$ROOT/scripts/collector_daemon.sh"
}

run_full_stack() {
  local fixed_pid=""
  local backfill_pid=""
  cleanup_children() {
    stop_pid "$backfill_pid"
    stop_pid "$fixed_pid"
  }
  trap cleanup_children EXIT INT TERM

  ROOT="$ROOT" \
  OUT_BASE="$OUT_BASE" \
  ENV_PATH="$ENV_PATH" \
  PYTHON_BIN="$PYTHON_BIN" \
  SOCKET_PATH="$SOCKET_PATH" \
  DEFAULT_STUDY_ID="$DEFAULT_STUDY_ID" \
  STUDY_ID="$STUDY_ID" \
  "$ROOT/scripts/collector_daemon.sh" &
  fixed_pid="$!"

  local attempt
  for attempt in $(seq 1 40); do
    if state_writer_responding; then
      break
    fi
    sleep 0.5
  done
  if ! state_writer_responding; then
    log_msg "fixed-panel daemon did not expose a healthy state-writer socket=$SOCKET_PATH"
    return 1
  fi

  ROOT="$ROOT" \
  OUT_BASE="$OUT_BASE" \
  PYTHON_BIN="$PYTHON_BIN" \
  BSKY_STATE_WRITER_SOCKET="$SOCKET_PATH" \
  LOG_LEVEL="${LOG_LEVEL:-info}" \
  LOOP_SLEEP_S="${LOOP_SLEEP_S:-30}" \
  INTERVAL_PUBLIC_OMNIBUS_S="${INTERVAL_PUBLIC_OMNIBUS_S:-300}" \
  RUN_ONCE="${RUN_ONCE:-0}" \
  RESUME="${RESUME:-1}" \
  DRY_RUN="${DRY_RUN:-0}" \
  SEED_REGISTRY="${SEED_REGISTRY:-1}" \
  INCLUDE_POSTS_FIRST_SEEN="${INCLUDE_POSTS_FIRST_SEEN:-1}" \
  ENQUEUE_INTERACTIONS_FROM_SEED="${ENQUEUE_INTERACTIONS_FROM_SEED:-1}" \
  ENQUEUE_RQ1_FACTORS_FROM_SEED="${ENQUEUE_RQ1_FACTORS_FROM_SEED:-1}" \
  RUN_INDEX_FEED_GENERATORS="${RUN_INDEX_FEED_GENERATORS:-0}" \
  RUN_REFRESH_DISCOVERY="${RUN_REFRESH_DISCOVERY:-0}" \
  RUN_BUILD_PANEL="${RUN_BUILD_PANEL:-0}" \
  RUN_SNAPSHOT_PANEL="${RUN_SNAPSHOT_PANEL:-0}" \
  RUN_WIDE_SWEEP="${RUN_WIDE_SWEEP:-0}" \
  RUN_HYDRATE_AUTHORS="${RUN_HYDRATE_AUTHORS:-0}" \
  RUN_HYDRATE_FEED_GENERATORS="${RUN_HYDRATE_FEED_GENERATORS:-0}" \
  RUN_BACKFILL_INTERACTIONS="${RUN_BACKFILL_INTERACTIONS:-1}" \
  RUN_BACKFILL_RQ1_FACTORS="${RUN_BACKFILL_RQ1_FACTORS:-1}" \
  RUN_MICRO_STUDIES="${RUN_MICRO_STUDIES:-0}" \
  "$ROOT/scripts/collector_public_omnivore_daemon.sh" &
  backfill_pid="$!"

  log_msg "full-stack started fixed_panel_pid=$fixed_pid realtime_backfill_pid=$backfill_pid socket=$SOCKET_PATH"
  wait "$fixed_pid" "$backfill_pid"
}

command="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$command" in
  fixed-panel)
    run_fixed_panel "$@"
    ;;
  realtime-backfill)
    run_realtime_backfill "$@"
    ;;
  history-backfill)
    run_history_backfill "$@"
    ;;
  full-stack)
    run_full_stack "$@"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
