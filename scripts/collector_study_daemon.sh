#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/Volumes/T9/BlueSky}"
OUT_BASE="${OUT_BASE:-$ROOT/data_v2_full}"
ENV_PATH="${ENV_PATH:-$ROOT/auth.env}"
PYTHON_BIN="${PYTHON_BIN:-}"
LOG_LEVEL="${LOG_LEVEL:-info}"

STUDY_ID="${STUDY_ID:-}"
SAMPLE_FAMILY="${SAMPLE_FAMILY:-}"
FROZEN_PANEL_PATH="${FROZEN_PANEL_PATH:-}"
SLEEP_UNTIL_WINDOW="${SLEEP_UNTIL_WINDOW:-1}"
STOP_ON_ERROR="${STOP_ON_ERROR:-0}"
LOOP_SLEEP_S="${LOOP_SLEEP_S:-1}"

AUX_REFRESH_DISCOVERY_CMD="${AUX_REFRESH_DISCOVERY_CMD:-}"
AUX_HYDRATE_AUTHORS_CMD="${AUX_HYDRATE_AUTHORS_CMD:-}"
AUX_WIDE_SWEEP_CMD="${AUX_WIDE_SWEEP_CMD:-}"

usage() {
  cat <<'EOF'
Usage:
  STUDY_ID=<study_id> ./scripts/collector_study_daemon.sh

Environment highlights:
  ROOT                  Repo root. Default: /Volumes/T9/BlueSky
  OUT_BASE              Canonical data root. Default: $ROOT/data_v2_full
  ENV_PATH              Optional auth env file for auth+unauth study mode.
  PYTHON_BIN            Python executable. Auto-detects .venv and .venv-win.
  STUDY_ID              Required frozen study id.
  SAMPLE_FAMILY         Optional expected sample family.
  FROZEN_PANEL_PATH     Optional explicit panel path check.
  SLEEP_UNTIL_WINDOW    Default 1. Align each run to the next window boundary.
  STOP_ON_ERROR         Default 0. Exit on the first failed window if set to 1.

This wrapper is for the dedicated paper-grade micro study loop, not the public-only omnivore path.
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

  printf 'collector_study_daemon.sh could not resolve a working python interpreter\n' >&2
  exit 2
}

PYTHON_BIN="$(resolve_python_bin)"

if [[ -z "$STUDY_ID" ]]; then
  printf 'collector_study_daemon.sh requires STUDY_ID\n' >&2
  exit 2
fi

cd "$ROOT"

LOG_DIR="$OUT_BASE/logs/study_daemon"
mkdir -p "$LOG_DIR"

ts_utc() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

log_msg() {
  printf '%s %s\n' "$(ts_utc)" "$*" | tee -a "$LOG_DIR/${STUDY_ID}.log"
}

run_aux_if_configured() {
  local name="$1"
  local cmd="$2"
  if [[ -z "$cmd" ]]; then
    return 0
  fi
  log_msg "starting auxiliary job name=$name"
  bash -lc "$cmd" >> "$LOG_DIR/${STUDY_ID}_${name}.log" 2>&1 &
}

log_msg "study daemon starting study_id=$STUDY_ID sample_family=${SAMPLE_FAMILY:-auto} out_base=$OUT_BASE"

while true; do
  cmd=(
    "$PYTHON_BIN" -m bsky_collector_v2 micro-snapshot-study
    --out-base "$OUT_BASE"
    --log-level "$LOG_LEVEL"
    --study-id "$STUDY_ID"
    --resume
  )

  if [[ -f "$ENV_PATH" ]]; then
    cmd+=(--env-path "$ENV_PATH")
  fi
  if [[ "$SLEEP_UNTIL_WINDOW" == "1" ]]; then
    cmd+=(--sleep-until-window)
  fi
  if [[ -n "$SAMPLE_FAMILY" ]]; then
    cmd+=(--sample-family "$SAMPLE_FAMILY")
  fi
  if [[ -n "$FROZEN_PANEL_PATH" ]]; then
    cmd+=(--frozen-panel-path "$FROZEN_PANEL_PATH")
  fi

  log_msg "starting micro window command=${cmd[*]}"
  set +e
  "${cmd[@]}" >> "$LOG_DIR/${STUDY_ID}.log" 2>&1
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    log_msg "micro window failed rc=$rc"
    if [[ "$STOP_ON_ERROR" == "1" ]]; then
      exit "$rc"
    fi
  else
    log_msg "micro window completed rc=0"
  fi

  run_aux_if_configured "refresh_discovery" "$AUX_REFRESH_DISCOVERY_CMD"
  run_aux_if_configured "hydrate_authors" "$AUX_HYDRATE_AUTHORS_CMD"
  run_aux_if_configured "wide_sweep" "$AUX_WIDE_SWEEP_CMD"

  sleep "$LOOP_SLEEP_S"
done
