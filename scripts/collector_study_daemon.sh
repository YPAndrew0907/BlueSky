#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/Volumes/T9/BlueSky}"
OUT_BASE="${OUT_BASE:-$ROOT/data_v2_full}"
ENV_PATH="${ENV_PATH:-$ROOT/auth.env}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
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
