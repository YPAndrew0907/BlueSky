#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/Volumes/T9/BlueSky}"
OUT_BASE="${OUT_BASE:-$ROOT/data_v2_full}"
ENV_PATH="${ENV_PATH:-$ROOT/auth.env}"
PYTHON_BIN="${PYTHON_BIN:-}"
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
# In micro5 mode the active collection path is the frozen 1500-feed study. Keep discovery-style
# auxiliary jobs opt-in so the fixed-panel loop does not drift or report unrelated health noise.
DEFAULT_AUX_JOBS_ENABLED="1"
if [[ "$COLLECTOR_MODE" == "micro5" ]]; then
  DEFAULT_AUX_JOBS_ENABLED="0"
fi
ENABLE_INDEX_FEED_GENERATORS="${ENABLE_INDEX_FEED_GENERATORS:-$DEFAULT_AUX_JOBS_ENABLED}"
ENABLE_HYDRATE_AUTHORS="${ENABLE_HYDRATE_AUTHORS:-1}"
ENABLE_REFRESH_DISCOVERY="${ENABLE_REFRESH_DISCOVERY:-$DEFAULT_AUX_JOBS_ENABLED}"
ENABLE_BUILD_PANEL="${ENABLE_BUILD_PANEL:-0}"
ENABLE_WIDE_SWEEP="${ENABLE_WIDE_SWEEP:-$DEFAULT_AUX_JOBS_ENABLED}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/collector_daemon.sh

Environment highlights:
  ROOT                 Repo root. Default: /Volumes/T9/BlueSky
  OUT_BASE             Canonical data root. Default: $ROOT/data_v2_full
  ENV_PATH             Optional auth env file for auth+unauth study mode.
  PYTHON_BIN           Python executable. Auto-detects .venv and .venv-win.
  COLLECTOR_MODE       micro5 | legacy_hourly. Default: micro5
  STUDY_ID             Frozen study id for micro5 mode.
  DEFAULT_STUDY_ID     Preferred study id when auto-discovering.
  SOCKET_PATH          Shared state-writer target. Unix path or tcp://HOST:PORT.

Notes:
  - micro5 mode is the paper-grade fixed-panel path.
  - in micro5 mode, discovery/index/wide auxiliary jobs are off by default.
  - on Windows/native Git Bash, prefer tcp://HOST:PORT for SOCKET_PATH.
EOF
}

case "${1:-}" in
  help|-h|--help)
    usage
    exit 0
    ;;
esac

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

  printf 'collector_daemon.sh could not resolve a working python interpreter\n' >&2
  exit 2
}

PYTHON_BIN="$(resolve_python_bin)"

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
  if [[ "${OS:-}" == "Windows_NT" ]] && command -v powershell.exe >/dev/null 2>&1; then
    local matched
    matched="$(
      JOB_PID="$pid" powershell.exe -NoProfile -Command '$proc = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$env:JOB_PID); if ($proc) { Write-Output 1 }' 2>/dev/null | tr -d '\r' | head -n 1
    )"
    [[ "$matched" == "1" ]]
    return
  fi
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

find_windows_child_pid() {
  local parent_pid="$1"
  local pattern="$2"
  [[ "${OS:-}" == "Windows_NT" ]] || return 1
  command -v powershell.exe >/dev/null 2>&1 || return 1
  local child_pid
  child_pid="$(
    JOB_PARENT_PID="$parent_pid" JOB_PATTERN="$pattern" powershell.exe -NoProfile -Command '$parentPid = [int]$env:JOB_PARENT_PID; $pattern = $env:JOB_PATTERN; $proc = Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $parentPid -and $_.CommandLine -like ("*" + $pattern + "*") } | Sort-Object CreationDate -Descending | Select-Object -First 1 -ExpandProperty ProcessId; if ($proc) { Write-Output $proc }' 2>/dev/null | tr -d '\r' | head -n 1
  )"
  [[ -n "$child_pid" ]] || return 1
  printf '%s\n' "$child_pid"
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

is_job_pid_alive() {
  local job="$1"
  local pid="$2"
  is_pid_alive "$pid" || return 1
  local pattern
  pattern="$(job_command_pattern "$job")"
  if [[ "${OS:-}" == "Windows_NT" ]] && command -v powershell.exe >/dev/null 2>&1; then
    local matched
    matched="$(
      JOB_PID="$pid" JOB_PATTERN="$pattern" powershell.exe -NoProfile -Command '$proc = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$env:JOB_PID); if ($proc -and $proc.CommandLine -like ("*" + $env:JOB_PATTERN + "*")) { Write-Output 1 }' 2>/dev/null | tr -d '\r' | head -n 1
    )"
    [[ "$matched" == "1" ]]
    return
  fi

  local cmd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$cmd" == *"$pattern"* ]]
}

adopt_job_pid() {
  local job="$1"
  local pid="$2"
  local pattern
  pattern="$(job_command_pattern "$job")"
  local recent_pid
  recent_pid="$(find_recent_windows_pid_by_pattern "$pattern" 30 2>/dev/null || true)"
  if [[ -n "$recent_pid" ]] && is_job_pid_alive "$job" "$recent_pid"; then
    printf '%s\n' "$recent_pid"
    return 0
  fi
  if is_job_pid_alive "$job" "$pid"; then
    printf '%s\n' "$pid"
    return 0
  fi
  local child_pid
  child_pid="$(find_windows_child_pid "$pid" "$pattern" 2>/dev/null || true)"
  [[ -n "$child_pid" ]] || return 1
  if is_job_pid_alive "$job" "$child_pid"; then
    printf '%s\n' "$child_pid"
    return 0
  fi
  return 1
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
  local active_pid=""
  if active_pid="$(adopt_job_pid "$job" "$pid" 2>/dev/null)"; then
    if [[ "$active_pid" != "$pid" ]]; then
      printf '%s\n' "$active_pid" > "$pf"
      log_msg "adopted child pid job=$job old_pid=$pid new_pid=$active_pid"
    fi
    return 0
  fi
  rm -f "$pf"
  log_msg "stale pid removed job=$job pid=$pid"
}

state_writer_responding() {
  "$PYTHON_BIN" - "$SOCKET_PATH" <<'PY' >/dev/null 2>&1
from __future__ import annotations

import json
import socket
import sys
from urllib.parse import urlparse

target = sys.argv[1].strip()
request = {
    "method": "ping",
    "args": [],
    "kwargs": {},
}
payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")

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
    if state_writer_responding; then
      log_msg "state-writer metadata stale pid=$pid target=$SOCKET_PATH kind=$(state_writer_target_kind) reusing_external_listener=1"
      remove_state_writer_metadata
      local active_pid=""
      active_pid="$(adopt_job_pid "state-writer" "$pid" 2>/dev/null || true)"
      if [[ -n "$active_pid" ]]; then
        printf '%s\n' "$active_pid" > "$CONTROL_DIR/state_writer.pid"
      fi
      printf '%s\n' "$SOCKET_PATH" > "$CONTROL_DIR/state_writer.socket"
      return 0
    fi
    log_msg "state-writer unhealthy pid=$pid target=$SOCKET_PATH kind=$(state_writer_target_kind)"
    stop_state_writer_process "$pid"
    remove_state_writer_metadata
  fi

  if state_writer_responding; then
    log_msg "state-writer already responding target=$SOCKET_PATH kind=$(state_writer_target_kind) reusing_external_listener=1"
    local active_pid=""
    active_pid="$(find_recent_windows_pid_by_pattern "$(job_command_pattern "state-writer")" 120 2>/dev/null || true)"
    if [[ -n "$active_pid" ]]; then
      printf '%s\n' "$active_pid" > "$CONTROL_DIR/state_writer.pid"
    fi
    printf '%s\n' "$SOCKET_PATH" > "$CONTROL_DIR/state_writer.socket"
    return 0
  fi

  remove_state_writer_target_artifact
  local ts
  ts="$(ts_utc_compact)"
  local log_file="$LOG_DIR/state-writer_${ts}.log"
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
  local writer_pid=$!

  printf '%s\n' "$writer_pid" > "$CONTROL_DIR/state_writer.pid"
  printf '%s\n' "$SOCKET_PATH" > "$CONTROL_DIR/state_writer.socket"
  printf '%s\n' "$log_file" > "$CONTROL_DIR/state_writer.logpath"

  local ready=0
  local _attempt
  for _attempt in $(seq 1 20); do
    if state_writer_responding; then
      local active_pid=""
      active_pid="$(adopt_job_pid "state-writer" "$writer_pid" 2>/dev/null || true)"
      if [[ -n "$active_pid" ]]; then
        writer_pid="$active_pid"
      fi
      printf '%s\n' "$writer_pid" > "$CONTROL_DIR/state_writer.pid"
      ready=1
      break
    fi
    sleep 0.5
  done

  if [[ "$ready" == "1" ]]; then
    log_msg "state-writer started pid=$writer_pid target=$SOCKET_PATH kind=$target_kind log=$log_file"
  else
    stop_state_writer_process "$writer_pid"
    remove_state_writer_metadata
    remove_state_writer_target_artifact
    log_msg "state-writer failed_to_start target=$SOCKET_PATH kind=$target_kind log=$log_file"
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

  local attempt
  for attempt in $(seq 1 20); do
    local active_pid=""
    active_pid="$(adopt_job_pid "$job" "$pid" 2>/dev/null || true)"
    if [[ -n "$active_pid" ]]; then
      if [[ "$active_pid" != "$pid" ]]; then
        printf '%s\n' "$active_pid" > "$(pid_file_for "$job")"
      fi
      log_msg "job_started job=$job pid=$active_pid log=$log_file"
      return 0
    fi
    sleep 0.5
  done

  # Quick-fail: retry soon instead of waiting full interval.
  local sf
  sf="$(stamp_file_for "$job")"
  echo $(( $(now_epoch) - 300 )) > "$sf"
  rm -f "$(pid_file_for "$job")"
  log_msg "job_failed_fast job=$job log=$log_file"
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
