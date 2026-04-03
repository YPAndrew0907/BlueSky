#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
OUT_BASE="${OUT_BASE:-$ROOT/data_v2_full}"
PYTHON_BIN="${PYTHON_BIN:-}"

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

  printf 'collector_public_omnivore_daemon.sh could not resolve a working python interpreter\n' >&2
  exit 2
}

PYTHON_BIN="$(resolve_python_bin)"

APPVIEW_HOST="${APPVIEW_HOST:-https://public.api.bsky.app}"
PDS_HOST="${PDS_HOST:-https://bsky.social}"
RELAY_HOST="${RELAY_HOST:-https://bsky.network}"

LOG_LEVEL="${LOG_LEVEL:-info}"
RPS="${RPS:-20}"
CONCURRENCY="${CONCURRENCY:-16}"
POSTS_PER_FEED="${POSTS_PER_FEED:-50}"
TIME_BUDGET_MINUTES="${TIME_BUDGET_MINUTES:-55}"
FEED_TIME_BUDGET_S="${FEED_TIME_BUDGET_S:-20}"
ACCEPT_LANGUAGE="${ACCEPT_LANGUAGE:-}"
ACCEPT_LABELERS="${ACCEPT_LABELERS:-}"
INCLUDE_AUTHOR_LABELS="${INCLUDE_AUTHOR_LABELS:-0}"
VANTAGE_ID_UNAUTH="${VANTAGE_ID_UNAUTH:-unauth}"

LOOP_SLEEP_S="${LOOP_SLEEP_S:-30}"
INTERVAL_PUBLIC_OMNIBUS_S="${INTERVAL_PUBLIC_OMNIBUS_S:-300}"
RUN_ONCE="${RUN_ONCE:-0}"
RESUME="${RESUME:-1}"
DRY_RUN="${DRY_RUN:-0}"

ALL_STUDIES="${ALL_STUDIES:-1}"
STUDY_IDS="${STUDY_IDS:-}"

SEED_REGISTRY="${SEED_REGISTRY:-1}"
INCLUDE_POSTS_FIRST_SEEN="${INCLUDE_POSTS_FIRST_SEEN:-1}"
ENQUEUE_INTERACTIONS_FROM_SEED="${ENQUEUE_INTERACTIONS_FROM_SEED:-1}"
ENQUEUE_RQ1_FACTORS_FROM_SEED="${ENQUEUE_RQ1_FACTORS_FROM_SEED:-1}"
RUN_INDEX_FEED_GENERATORS="${RUN_INDEX_FEED_GENERATORS:-1}"
RUN_REFRESH_DISCOVERY="${RUN_REFRESH_DISCOVERY:-1}"
RUN_BUILD_PANEL="${RUN_BUILD_PANEL:-1}"
RUN_SNAPSHOT_PANEL="${RUN_SNAPSHOT_PANEL:-1}"
RUN_WIDE_SWEEP="${RUN_WIDE_SWEEP:-1}"
RUN_HYDRATE_AUTHORS="${RUN_HYDRATE_AUTHORS:-1}"
RUN_HYDRATE_FEED_GENERATORS="${RUN_HYDRATE_FEED_GENERATORS:-1}"
RUN_BACKFILL_INTERACTIONS="${RUN_BACKFILL_INTERACTIONS:-1}"
RUN_BACKFILL_RQ1_FACTORS="${RUN_BACKFILL_RQ1_FACTORS:-1}"
RUN_MICRO_STUDIES="${RUN_MICRO_STUDIES:-1}"

SEED_MAX_FILES="${SEED_MAX_FILES:-0}"
SEED_MAX_ROWS="${SEED_MAX_ROWS:-0}"
N_FEEDS_WIDE="${N_FEEDS_WIDE:-5000}"
MAX_AUTHORS="${MAX_AUTHORS:-200000}"
MAX_FEED_GENERATORS="${MAX_FEED_GENERATORS:-200000}"
MAX_POSTS_INTERACTIONS="${MAX_POSTS_INTERACTIONS:-200000}"
MAX_POSTS_RQ1="${MAX_POSTS_RQ1:-200000}"
BATCH_SIZE_INTERACTIONS="${BATCH_SIZE_INTERACTIONS:-25}"
BATCH_SIZE_RQ1="${BATCH_SIZE_RQ1:-25}"
MAX_ITEMS_PER_ENDPOINT_INTERACTIONS="${MAX_ITEMS_PER_ENDPOINT_INTERACTIONS:-0}"
MAX_ITEMS_PER_ENDPOINT_RQ1="${MAX_ITEMS_PER_ENDPOINT_RQ1:-0}"
MAX_THREAD_DEPTH="${MAX_THREAD_DEPTH:-1000}"
MAX_THREAD_PARENT_HEIGHT="${MAX_THREAD_PARENT_HEIGHT:-1000}"
MAX_AUTHOR_FEED_ITEMS="${MAX_AUTHOR_FEED_ITEMS:-0}"
MAX_FOLLOWERS_PER_ACTOR="${MAX_FOLLOWERS_PER_ACTOR:-0}"
MAX_FOLLOWS_PER_ACTOR="${MAX_FOLLOWS_PER_ACTOR:-0}"
MAX_FOLLOW_RECORDS_PER_ACTOR="${MAX_FOLLOW_RECORDS_PER_ACTOR:-0}"
MAX_ACTOR_FEEDS_PER_ACTOR="${MAX_ACTOR_FEEDS_PER_ACTOR:-0}"
MAX_LISTS_PER_ACTOR="${MAX_LISTS_PER_ACTOR:-0}"
MAX_LIST_MEMBERS_PER_LIST="${MAX_LIST_MEMBERS_PER_LIST:-0}"
MAX_STARTER_PACKS_PER_ACTOR="${MAX_STARTER_PACKS_PER_ACTOR:-0}"
SEEN_AFTER_UTC="${SEEN_AFTER_UTC:-}"
SEEN_BEFORE_UTC="${SEEN_BEFORE_UTC:-}"
INCLUDE_HYDRATED_INTERACTIONS="${INCLUDE_HYDRATED_INTERACTIONS:-0}"
INCLUDE_HYDRATED_RQ1="${INCLUDE_HYDRATED_RQ1:-0}"
RESOLVE_PDS_ENDPOINTS="${RESOLVE_PDS_ENDPOINTS:-1}"
FOLLOW_RECORD_SCOPE="${FOLLOW_RECORD_SCOPE:-seed+graph}"
SHARD_INDEX="${SHARD_INDEX:-0}"
SHARD_COUNT="${SHARD_COUNT:-1}"
PANEL_K1_POPULAR="${PANEL_K1_POPULAR:-700}"
PANEL_K2_ONBOARDING="${PANEL_K2_ONBOARDING:-300}"
PANEL_K3_SUGGESTED="${PANEL_K3_SUGGESTED:-300}"
PANEL_K4_LONGTAIL="${PANEL_K4_LONGTAIL:-200}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/collector_public_omnivore_daemon.sh

This wrapper is environment-driven. Typical sanity run:

  ROOT=/Volumes/T9/BlueSky \
  OUT_BASE=/Volumes/T9/BlueSky/data_v2_full \
  RUN_ONCE=1 \
  DRY_RUN=1 \
  ./scripts/collector_public_omnivore_daemon.sh

Key environment variables:
  RUN_ONCE                    Exit after one cycle.
  DRY_RUN                     Pass --dry-run to collect-public-omnibus.
  INTERVAL_PUBLIC_OMNIBUS_S   Cycle interval in seconds.
  SEED_REGISTRY               Enable history seeding.
  RUN_BACKFILL_INTERACTIONS   Enable interaction backfill.
  RUN_BACKFILL_RQ1_FACTORS    Enable RQ1 factor backfill.
  RUN_MICRO_STUDIES           Enable public-only micro windows.

Notes:
  - viewer mode is always forced to unauth by collect-public-omnibus.
  - this wrapper is public-only and not equivalent to the auth+unauth fixed-panel study.
EOF
}

case "${1:-}" in
  help|-h|--help)
    usage
    exit 0
    ;;
esac

CONTROL_DIR="$OUT_BASE/control"
LOG_DIR="$OUT_BASE/logs/manual_runs"
PID_FILE="$CONTROL_DIR/collector_public_omnivore_daemon.pid"
STAMP_FILE="$CONTROL_DIR/public_omnivore.last_start_epoch"
mkdir -p "$CONTROL_DIR" "$LOG_DIR"
printf '%s\n' "$$" > "$PID_FILE"

cleanup() {
  local cur
  cur="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$cur" == "$$" ]]; then
    rm -f "$PID_FILE"
  fi
}
trap cleanup EXIT

log_msg() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_DIR/collector_public_omnivore_daemon.log" >/dev/null
}

now_epoch() {
  date '+%s'
}

is_due() {
  if [[ ! -f "$STAMP_FILE" ]]; then
    return 0
  fi
  local last
  last="$(cat "$STAMP_FILE" 2>/dev/null || echo 0)"
  [[ -z "$last" ]] && return 0
  local now
  now="$(now_epoch)"
  (( now - last >= INTERVAL_PUBLIC_OMNIBUS_S ))
}

mark_started() {
  now_epoch > "$STAMP_FILE"
}

bool_flag() {
  local value="${1:-0}"
  local positive="${2:?missing positive flag}"
  local negative="${3:?missing negative flag}"
  if [[ "$value" == "1" || "$value" == "true" || "$value" == "TRUE" || "$value" == "yes" || "$value" == "YES" ]]; then
    printf '%s\n' "$positive"
  else
    printf '%s\n' "$negative"
  fi
}

run_cycle() {
  local ts
  ts="$(date -u '+%Y%m%dT%H%M%SZ')"
  local cycle_log="$LOG_DIR/public_omnivore_${ts}.log"
  local -a cmd=(
    "$PYTHON_BIN" -m bsky_collector_v2 collect-public-omnibus
    --out-base "$OUT_BASE"
    --appview-host "$APPVIEW_HOST"
    --pds-host "$PDS_HOST"
    --relay-host "$RELAY_HOST"
    --log-level "$LOG_LEVEL"
    --rps "$RPS"
    --concurrency "$CONCURRENCY"
    --posts-per-feed "$POSTS_PER_FEED"
    --time-budget-minutes "$TIME_BUDGET_MINUTES"
    --feed-time-budget-s "$FEED_TIME_BUDGET_S"
    --vantage-id-unauth "$VANTAGE_ID_UNAUTH"
    --seed-max-files "$SEED_MAX_FILES"
    --seed-max-rows "$SEED_MAX_ROWS"
    --n-feeds-wide "$N_FEEDS_WIDE"
    --max-authors "$MAX_AUTHORS"
    --max-feed-generators "$MAX_FEED_GENERATORS"
    --max-posts-interactions "$MAX_POSTS_INTERACTIONS"
    --max-posts-rq1 "$MAX_POSTS_RQ1"
    --batch-size-interactions "$BATCH_SIZE_INTERACTIONS"
    --batch-size-rq1 "$BATCH_SIZE_RQ1"
    --max-items-per-endpoint-interactions "$MAX_ITEMS_PER_ENDPOINT_INTERACTIONS"
    --max-items-per-endpoint-rq1 "$MAX_ITEMS_PER_ENDPOINT_RQ1"
    --max-thread-depth "$MAX_THREAD_DEPTH"
    --max-thread-parent-height "$MAX_THREAD_PARENT_HEIGHT"
    --max-author-feed-items "$MAX_AUTHOR_FEED_ITEMS"
    --max-followers-per-actor "$MAX_FOLLOWERS_PER_ACTOR"
    --max-follows-per-actor "$MAX_FOLLOWS_PER_ACTOR"
    --max-follow-records-per-actor "$MAX_FOLLOW_RECORDS_PER_ACTOR"
    --max-actor-feeds-per-actor "$MAX_ACTOR_FEEDS_PER_ACTOR"
    --max-lists-per-actor "$MAX_LISTS_PER_ACTOR"
    --max-list-members-per-list "$MAX_LIST_MEMBERS_PER_LIST"
    --max-starter-packs-per-actor "$MAX_STARTER_PACKS_PER_ACTOR"
    --follow-record-scope "$FOLLOW_RECORD_SCOPE"
    --shard-index "$SHARD_INDEX"
    --shard-count "$SHARD_COUNT"
    --panel-k1-popular "$PANEL_K1_POPULAR"
    --panel-k2-onboarding "$PANEL_K2_ONBOARDING"
    --panel-k3-suggested "$PANEL_K3_SUGGESTED"
    --panel-k4-longtail "$PANEL_K4_LONGTAIL"
  )

  if [[ -n "$ACCEPT_LANGUAGE" ]]; then
    cmd+=(--accept-language "$ACCEPT_LANGUAGE")
  fi
  if [[ -n "$ACCEPT_LABELERS" ]]; then
    cmd+=(--accept-labelers "$ACCEPT_LABELERS")
  fi
  if [[ -n "$SEEN_AFTER_UTC" ]]; then
    cmd+=(--seen-after-utc "$SEEN_AFTER_UTC")
  fi
  if [[ -n "$SEEN_BEFORE_UTC" ]]; then
    cmd+=(--seen-before-utc "$SEEN_BEFORE_UTC")
  fi
  if [[ "$RESUME" == "1" ]]; then
    cmd+=(--resume)
  else
    cmd+=(--no-resume)
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    cmd+=(--dry-run)
  fi
  cmd+=("$(bool_flag "$INCLUDE_AUTHOR_LABELS" --include-author-labels --no-include-author-labels)")
  cmd+=("$(bool_flag "$ALL_STUDIES" --all-studies --no-all-studies)")
  cmd+=("$(bool_flag "$SEED_REGISTRY" --seed-registry --no-seed-registry)")
  cmd+=("$(bool_flag "$INCLUDE_POSTS_FIRST_SEEN" --include-posts-first-seen --no-include-posts-first-seen)")
  cmd+=("$(bool_flag "$ENQUEUE_INTERACTIONS_FROM_SEED" --enqueue-interactions-from-seed --no-enqueue-interactions-from-seed)")
  cmd+=("$(bool_flag "$ENQUEUE_RQ1_FACTORS_FROM_SEED" --enqueue-rq1-factors-from-seed --no-enqueue-rq1-factors-from-seed)")
  cmd+=("$(bool_flag "$RUN_INDEX_FEED_GENERATORS" --run-index-feed-generators --no-run-index-feed-generators)")
  cmd+=("$(bool_flag "$RUN_REFRESH_DISCOVERY" --run-refresh-discovery --no-run-refresh-discovery)")
  cmd+=("$(bool_flag "$RUN_BUILD_PANEL" --run-build-panel --no-run-build-panel)")
  cmd+=("$(bool_flag "$RUN_SNAPSHOT_PANEL" --run-snapshot-panel --no-run-snapshot-panel)")
  cmd+=("$(bool_flag "$RUN_WIDE_SWEEP" --run-wide-sweep --no-run-wide-sweep)")
  cmd+=("$(bool_flag "$RUN_HYDRATE_AUTHORS" --run-hydrate-authors --no-run-hydrate-authors)")
  cmd+=("$(bool_flag "$RUN_HYDRATE_FEED_GENERATORS" --run-hydrate-feed-generators --no-run-hydrate-feed-generators)")
  cmd+=("$(bool_flag "$RUN_BACKFILL_INTERACTIONS" --run-backfill-interactions --no-run-backfill-interactions)")
  cmd+=("$(bool_flag "$RUN_BACKFILL_RQ1_FACTORS" --run-backfill-rq1-factors --no-run-backfill-rq1-factors)")
  cmd+=("$(bool_flag "$RUN_MICRO_STUDIES" --run-micro-studies --no-run-micro-studies)")
  cmd+=("$(bool_flag "$INCLUDE_HYDRATED_INTERACTIONS" --include-hydrated-interactions --no-include-hydrated-interactions)")
  cmd+=("$(bool_flag "$INCLUDE_HYDRATED_RQ1" --include-hydrated-rq1 --no-include-hydrated-rq1)")
  cmd+=("$(bool_flag "$RESOLVE_PDS_ENDPOINTS" --resolve-pds-endpoints --no-resolve-pds-endpoints)")

  if [[ -n "$STUDY_IDS" ]]; then
    IFS=',' read -r -a study_array <<< "$STUDY_IDS"
    for study_id in "${study_array[@]}"; do
      study_id="${study_id//[[:space:]]/}"
      [[ -n "$study_id" ]] || continue
      cmd+=(--study-id "$study_id")
    done
  fi

  log_msg "starting public omnivore cycle out_base=$OUT_BASE log=$cycle_log"
  (
    cd "$ROOT"
    printf 'COMMAND:'
    printf ' %q' "${cmd[@]}"
    printf '\n\n'
    "${cmd[@]}"
  ) >> "$cycle_log" 2>&1
  log_msg "completed public omnivore cycle log=$cycle_log"
}

cd "$ROOT"
log_msg "public omnivore daemon started root=$ROOT out_base=$OUT_BASE interval_s=$INTERVAL_PUBLIC_OMNIBUS_S"

while true; do
  if is_due; then
    mark_started
    if ! run_cycle; then
      log_msg "public omnivore cycle failed"
      if [[ "$RUN_ONCE" == "1" ]]; then
        exit 1
      fi
    else
      if [[ "$RUN_ONCE" == "1" ]]; then
        exit 0
      fi
    fi
  fi
  sleep "$LOOP_SLEEP_S"
done
