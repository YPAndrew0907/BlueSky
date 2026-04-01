#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/Volumes/T9/BlueSky}"
OUT_BASE="${OUT_BASE:-$ROOT/data_v2_full}"
ENV_PATH="${ENV_PATH:-$ROOT/auth.env}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
COLLECTOR_MODE="${COLLECTOR_MODE:-micro5}"
DEFAULT_STUDY_ID="${DEFAULT_STUDY_ID:-micro10_full_live_20260319}"
STUDY_ID="${STUDY_ID:-}"

SOCKET_PATH="${SOCKET_PATH:-/tmp/bsky_state_writer_prod.sock}"
CONTROL_DIR="$OUT_BASE/control"
LOG_DIR="$OUT_BASE/logs/manual_runs"
LAUNCHD_LOG_DIR="$OUT_BASE/logs/launchd"
STATE_DIR="$CONTROL_DIR/daemon_state"
DAEMON_PID_FILE="$CONTROL_DIR/collector_daemon.pid"

LOOP_SLEEP_S="${LOOP_SLEEP_S:-30}"

# Scheduling intervals (seconds)
INTERVAL_MICRO_S="${INTERVAL_MICRO_S:-600}"            # every 10 min
INTERVAL_SNAPSHOT_S="${INTERVAL_SNAPSHOT_S:-3600}"      # hourly
INTERVAL_INDEX_S="${INTERVAL_INDEX_S:-3600}"            # hourly
INTERVAL_HYDRATE_S="${INTERVAL_HYDRATE_S:-10800}"       # every 3h
INTERVAL_REFRESH_S="${INTERVAL_REFRESH_S:-86400}"       # daily
INTERVAL_BUILD_PANEL_S="${INTERVAL_BUILD_PANEL_S:-86400}"  # daily
INTERVAL_WIDE_S="${INTERVAL_WIDE_S:-86400}"             # daily
FORCE_FULL_COLLECTION_ON_START="${FORCE_FULL_COLLECTION_ON_START:-1}"
ENABLE_INDEX_FEED_GENERATORS="${ENABLE_INDEX_FEED_GENERATORS:-1}"
ENABLE_HYDRATE_AUTHORS="${ENABLE_HYDRATE_AUTHORS:-1}"
ENABLE_REFRESH_DISCOVERY="${ENABLE_REFRESH_DISCOVERY:-1}"
ENABLE_BUILD_PANEL="${ENABLE_BUILD_PANEL:-0}"
ENABLE_WIDE_SWEEP="${ENABLE_WIDE_SWEEP:-1}"

# Re-anchor to the mounted repo before touching any paths. External-drive ejects
# can leave the wrapper shell in a deleted cwd, which breaks resumable Python jobs.
cd "$ROOT"

mkdir -p "$CONTROL_DIR" "$LOG_DIR" "$LAUNCHD_LOG_DIR" "$STATE_DIR"
printf '%s\n' "$$" > "$DAEMON_PID_FILE"

log_msg() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$LAUNCHD_LOG_DIR/collector-daemon.log"
}

on_fatal_error() {
  local rc="$1"
  local line="$2"
  local cmd="${3:-}"
  log_msg "fatal_error rc=$rc line=$line cmd=$cmd"
  exit "$rc"
}

cleanup_pid_file() {
  local cur
  cur="$(cat "$DAEMON_PID_FILE" 2>/dev/null || true)"
  if [[ "$cur" == "$$" ]]; then
    rm -f "$DAEMON_PID_FILE"
  fi
}

remove_state_writer_metadata() {
  rm -f "$CONTROL_DIR/state_writer.pid" \
    "$CONTROL_DIR/state_writer.socket" \
    "$CONTROL_DIR/state_writer.logpath"
}

stop_state_writer_process() {
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

trap 'on_fatal_error "$?" "$LINENO" "${BASH_COMMAND:-}"' ERR
trap 'cleanup_pid_file' EXIT

ts_utc_compact() {
  date -u '+%Y%m%dT%H%M%SZ'
}

now_epoch() {
  date '+%s'
}

discover_default_study_id() {
  if [[ -n "$DEFAULT_STUDY_ID" && -f "$OUT_BASE/studies/$DEFAULT_STUDY_ID/study_manifest.json" ]]; then
    printf '%s\n' "$DEFAULT_STUDY_ID"
    return 0
  fi
  "$PYTHON_BIN" - "$OUT_BASE" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

out_base = Path(sys.argv[1])
manifests = sorted(out_base.glob("studies/*/study_manifest.json"))
core_candidates: list[tuple[str, str]] = []
all_candidates: list[tuple[str, str]] = []
for path in manifests:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    study_id = str(data.get("study_id") or path.parent.name)
    created_at = str(data.get("created_at_utc") or "")
    all_candidates.append((created_at, study_id))
    if str(data.get("sample_family") or "") == "micro5_core_full":
        core_candidates.append((created_at, study_id))

candidates = core_candidates or all_candidates
if not candidates:
    raise SystemExit(0)

created_at, study_id = max(candidates)
print(study_id)
PY
}

require_micro5_config() {
  if [[ -z "$STUDY_ID" ]]; then
    STUDY_ID="$(discover_default_study_id)"
  fi
  if [[ -z "$STUDY_ID" ]]; then
    log_msg "micro5 config missing no study manifest found under $OUT_BASE/studies and STUDY_ID is unset"
    exit 2
  fi
  local manifest_path="$OUT_BASE/studies/$STUDY_ID/study_manifest.json"
  if [[ ! -f "$manifest_path" ]]; then
    log_msg "micro5 config missing study_id=$STUDY_ID manifest=$manifest_path"
    exit 2
  fi
}

pid_file_for() {
  local job="$1"
  if [[ "$job" == "state-writer" ]]; then
    printf '%s\n' "$CONTROL_DIR/state_writer.pid"
    return
  fi
  printf '%s\n' "$CONTROL_DIR/${job}.pid"
}

is_pid_alive() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

job_command_pattern() {
  local job="$1"
  if [[ "$job" == "state-writer" ]]; then
    printf '%s\n' "-m bsky_collector_v2 state-writer"
    return
  fi
  printf '%s\n' "-m bsky_collector_v2 $job"
}

is_job_pid_alive() {
  local job="$1"
  local pid="$2"
  is_pid_alive "$pid" || return 1
  local cmd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  local pattern
  pattern="$(job_command_pattern "$job")"
  [[ "$cmd" == *"$pattern"* ]]
}

read_pid() {
  local job="$1"
  local pf
  pf="$(pid_file_for "$job")"
  [[ -f "$pf" ]] || return 1
  cat "$pf"
}

clear_stale_pid() {
  local job="$1"
  local pf
  pf="$(pid_file_for "$job")"
  [[ -f "$pf" ]] || return 0
  local pid
  pid="$(cat "$pf" 2>/dev/null || true)"
  if ! is_job_pid_alive "$job" "$pid"; then
    rm -f "$pf"
    log_msg "stale pid removed job=$job pid=$pid"
  fi
}

state_writer_responding() {
  [[ -S "$SOCKET_PATH" ]] || return 1
  "$PYTHON_BIN" - "$SOCKET_PATH" <<'PY' >/dev/null 2>&1
from __future__ import annotations

import json
import socket
import sys

socket_path = sys.argv[1]
request = {
    "method": "list_feed_catalog_uris",
    "args": [],
    "kwargs": {"limit": 1},
}
payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
    conn.settimeout(2.0)
    conn.connect(socket_path)
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

stamp_file_for() {
  local job="$1"
  printf '%s\n' "$STATE_DIR/${job}.last_start_epoch"
}

is_due() {
  local job="$1"
  local interval_s="$2"
  local sf
  sf="$(stamp_file_for "$job")"
  local now
  now="$(now_epoch)"
  if [[ ! -f "$sf" ]]; then
    return 0
  fi
  local last
  last="$(cat "$sf" 2>/dev/null || echo 0)"
  if [[ -z "$last" ]]; then
    return 0
  fi
  (( now - last >= interval_s ))
}

mark_started() {
  local job="$1"
  local sf
  sf="$(stamp_file_for "$job")"
  now_epoch > "$sf"
}

reset_schedule_stamps_for_full_start() {
  if [[ "$FORCE_FULL_COLLECTION_ON_START" != "1" ]]; then
    log_msg "startup stamp reset disabled FORCE_FULL_COLLECTION_ON_START=$FORCE_FULL_COLLECTION_ON_START"
    return 0
  fi
  local jobs
  jobs=("index-feed-generators" "hydrate-authors" "refresh-discovery" "build-panel" "wide-sweep")
  if [[ "$COLLECTOR_MODE" == "micro5" ]]; then
    jobs+=("micro-snapshot-study")
  else
    jobs+=("snapshot-panel")
  fi
  local cleared=0
  local job sf
  for job in "${jobs[@]}"; do
    sf="$(stamp_file_for "$job")"
    if [[ -f "$sf" ]]; then
      rm -f "$sf"
      cleared=$((cleared + 1))
    fi
  done
  log_msg "startup stamp reset enabled cleared=$cleared"
}

ensure_state_writer() {
  clear_stale_pid "state-writer"
  local pid=""
  if pid="$(read_pid "state-writer" 2>/dev/null)"; then
    if is_job_pid_alive "state-writer" "$pid" && state_writer_responding; then
      return 0
    fi
    log_msg "state-writer unhealthy pid=$pid socket_present=$([[ -S "$SOCKET_PATH" ]] && echo 1 || echo 0)"
    stop_state_writer_process "$pid"
    remove_state_writer_metadata
  fi

  rm -f "$SOCKET_PATH"
  local ts
  ts="$(ts_utc_compact)"
  local log_file="$LOG_DIR/state-writer_${ts}.log"

  "$PYTHON_BIN" -m bsky_collector_v2 state-writer \
    --out-base "$OUT_BASE" \
    --socket-path "$SOCKET_PATH" \
    > "$log_file" 2>&1 &
  local writer_pid=$!

  printf '%s\n' "$writer_pid" > "$CONTROL_DIR/state_writer.pid"
  printf '%s\n' "$SOCKET_PATH" > "$CONTROL_DIR/state_writer.socket"
  printf '%s\n' "$log_file" > "$CONTROL_DIR/state_writer.logpath"

  local ready=0
  local _attempt
  for _attempt in $(seq 1 20); do
    if is_job_pid_alive "state-writer" "$writer_pid" && state_writer_responding; then
      ready=1
      break
    fi
    sleep 0.5
  done

  if [[ "$ready" == "1" ]]; then
    log_msg "state-writer started pid=$writer_pid socket=$SOCKET_PATH log=$log_file"
  else
    stop_state_writer_process "$writer_pid"
    remove_state_writer_metadata
    rm -f "$SOCKET_PATH"
    log_msg "state-writer failed_to_start log=$log_file"
  fi
}

start_job_now() {
  local job="$1"
  shift
  local ts
  ts="$(ts_utc_compact)"
  local log_file="$LOG_DIR/${job}_${ts}.log"

  BSKY_STATE_WRITER_SOCKET="$SOCKET_PATH" "$PYTHON_BIN" -m bsky_collector_v2 "$@" > "$log_file" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" > "$(pid_file_for "$job")"
  mark_started "$job"

  sleep 1
  if is_job_pid_alive "$job" "$pid"; then
    log_msg "job_started job=$job pid=$pid log=$log_file"
  else
    # Quick-fail: retry soon instead of waiting full interval.
    local sf
    sf="$(stamp_file_for "$job")"
    echo $(( $(now_epoch) - 300 )) > "$sf"
    rm -f "$(pid_file_for "$job")"
    log_msg "job_failed_fast job=$job log=$log_file"
  fi
}

maybe_start_interval_job() {
  local job="$1"
  local interval_s="$2"
  shift 2

  # Dependency: build-panel should run *after* refresh-discovery so popularity/onboarding
  # signals are current. Otherwise a daemon restart can cause build-panel to run immediately
  # using stale metadata.
  if [[ "$job" == "build-panel" ]]; then
    clear_stale_pid "refresh-discovery"
    local dep_pid=""
    if dep_pid="$(read_pid "refresh-discovery" 2>/dev/null)"; then
      if is_job_pid_alive "refresh-discovery" "$dep_pid"; then
        return 0
      fi
    fi
  fi

  clear_stale_pid "$job"
  local pid=""
  if pid="$(read_pid "$job" 2>/dev/null)"; then
    if is_job_pid_alive "$job" "$pid"; then
      return 0
    fi
  fi
  if ! is_due "$job" "$interval_s"; then
    return 0
  fi
  start_job_now "$job" "$@"
}

if [[ "$COLLECTOR_MODE" == "micro5" ]]; then
  require_micro5_config
fi

log_msg "collector-daemon starting pid=$$ mode=$COLLECTOR_MODE root=$ROOT out_base=$OUT_BASE study_id=${STUDY_ID:-}"
reset_schedule_stamps_for_full_start

while true; do
  ensure_state_writer

  if [[ "$COLLECTOR_MODE" == "micro5" ]]; then
    maybe_start_interval_job "micro-snapshot-study" "$INTERVAL_MICRO_S" \
      micro-snapshot-study \
      --out-base "$OUT_BASE" \
      --env-path "$ENV_PATH" \
      --study-id "$STUDY_ID" \
      --sleep-until-window \
      --concurrency 16 \
      --rps 20 \
      --feed-time-budget-s 20 \
      --resume
  elif [[ "$COLLECTOR_MODE" == "legacy_hourly" ]]; then
    maybe_start_interval_job "snapshot-panel" "$INTERVAL_SNAPSHOT_S" \
      snapshot-panel \
      --out-base "$OUT_BASE" \
      --env-path "$ENV_PATH" \
      --accept-language en-US \
      --vantage-id-unauth unauth_enUS \
      --vantage-id-auth auth_enUS \
      --viewer-modes unauth,auth \
      --posts-per-feed 50 \
      --concurrency 16 \
      --rps 20 \
      --feed-time-budget-s 20 \
      --time-budget-minutes 55 \
      --resume
  else
    log_msg "invalid collector mode mode=$COLLECTOR_MODE expected=micro5|legacy_hourly"
    exit 2
  fi

  if [[ "$ENABLE_INDEX_FEED_GENERATORS" == "1" ]]; then
    maybe_start_interval_job "index-feed-generators" "$INTERVAL_INDEX_S" \
      index-feed-generators \
      --out-base "$OUT_BASE" \
      --env-path "$ENV_PATH" \
      --relay-host https://bsky.network \
      --pds-host https://bsky.social \
      --rps 20 \
      --time-budget-minutes 55 \
      --resume
  fi

  if [[ "$ENABLE_HYDRATE_AUTHORS" == "1" ]]; then
    maybe_start_interval_job "hydrate-authors" "$INTERVAL_HYDRATE_S" \
      hydrate-authors \
      --out-base "$OUT_BASE" \
      --env-path "$ENV_PATH" \
      --accept-language en-US \
      --vantage-id-unauth unauth_enUS \
      --max-authors 50000 \
      --batch-size 25 \
      --concurrency 8 \
      --rps 20 \
      --resume
  fi

  if [[ "$ENABLE_REFRESH_DISCOVERY" == "1" ]]; then
    maybe_start_interval_job "refresh-discovery" "$INTERVAL_REFRESH_S" \
      refresh-discovery \
      --out-base "$OUT_BASE" \
      --env-path "$ENV_PATH" \
      --accept-language en-US \
      --vantage-id-unauth unauth_enUS \
      --vantage-id-auth auth_enUS \
      --concurrency 16 \
      --rps 20 \
      --resume
  fi

  if [[ "$ENABLE_BUILD_PANEL" == "1" ]]; then
    maybe_start_interval_job "build-panel" "$INTERVAL_BUILD_PANEL_S" \
      build-panel \
      --out-base "$OUT_BASE" \
      --env-path "$ENV_PATH" \
      --concurrency 16 \
      --rps 20
  fi

  if [[ "$ENABLE_WIDE_SWEEP" == "1" ]]; then
    maybe_start_interval_job "wide-sweep" "$INTERVAL_WIDE_S" \
      wide-sweep \
      --out-base "$OUT_BASE" \
      --env-path "$ENV_PATH" \
      --accept-language en-US \
      --vantage-id-unauth unauth_enUS \
      --posts-per-feed 20 \
      --n-feeds 10000 \
      --concurrency 16 \
      --rps 20 \
      --feed-time-budget-s 20 \
      --time-budget-minutes 55 \
      --resume
  fi

  sleep "$LOOP_SLEEP_S"
done
