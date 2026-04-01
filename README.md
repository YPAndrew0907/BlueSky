# Bluesky Collector v2 (data_v2_full)

Production-grade, crash-resilient, write-as-you-go collector for feed impressions + discovery metadata.

## Hard guarantees

- Writes incrementally to disk under `/Volumes/T9/BlueSky/data_v2_full` (no “write everything at the end”).
- `kill -9` at any time does **not** delete already-written parts/logs/DBs.
- Resume is the default: reruns continue from per-run SQLite state with minimal duplication.
- Fails fast if `--out-base` is missing or not writable (never silently writes elsewhere).
- No secrets are written to logs.

## Output layout (under `--out-base`)

```
metadata/YYYY-MM-DD/
  run_manifest.json
  discovery_status.json
  quality_report.json
  progress.json
  request_provenance.csv
  auth_preference_snapshot.json        # present when auth vantage is used
  discovery_sources/
    popular_feed_generators.jsonl
    suggested_feeds.jsonl
    suggested_accounts.jsonl
    suggested_follows_by_actor.jsonl
    onboarding_suggested_starterpacks.jsonl
  feed_catalog.csv
  starterpack_feeds.csv
  starterpack_accounts.csv
  suggested_feeds.csv
  suggested_accounts.csv
  suggested_follows_by_actor.csv
  feed_generators_index/
    run_manifest.json
    quality_report.json
    progress.json
    http_stats.csv
    request_provenance.csv
    auth_preference_snapshot.json      # present when auth is used
    parts/
      feed_generators_part_000000.jsonl
      ...
    logs/
      index.log

panel/
  panel_v1.csv
  panel_versions/
    panel_v1_YYYY-MM-DD.csv

studies/
  benchmarks/
    bench_<id>.json
  <study_id>/
    study_manifest.json
    benchmark_result.json
    panel/
      frozen_panel.csv

hourly/YYYY-MM-DD/HH/
  run_manifest.json
  quality_report.json
  progress.json
  http_stats.csv
  request_provenance.csv
  auth_preference_snapshot.json        # present when auth vantage is used
  snapshot_status.sqlite
  parts/
    feed_items_part_000.csv
    posts_first_seen_part_000.csv
    post_labels_part_000.csv
    post_metrics_part_000.csv
  logs/
    snapshot.log

wide/YYYY-MM-DD/
  run_manifest.json
  quality_report.json
  progress.json
  http_stats.csv
  request_provenance.csv
  parts/...
  logs/
    wide.log

micro5/<study_id>/<sample_family>/YYYY-MM-DD/HH/MM/
  run_manifest.json
  quality_report.json
  progress.json
  http_stats.csv
  request_provenance.csv
  auth_preference_snapshot.json      # present when auth vantage is used
  snapshot_status.sqlite
  parts/
    feed_items_part_000.csv
    posts_first_seen_part_000.csv
    post_labels_part_000.csv
    post_metrics_part_000.csv
  logs/
    snapshot.log

authors/YYYY-MM-DD/
  run_manifest.json
  quality_report.json
  progress.json
  http_stats.csv
  request_provenance.csv
  author_profiles_part_000.csv

control/
  control_state.db
  feed_generators_index_checkpoint.json
  auth_sessions/
    *.json                            # cached refresh/access tokens for session reuse

logs/
  collector.log
  errors.log

effective_csv/
  key/                                  # latest non-empty "key" CSVs
    metadata/*.csv
    panel/panel_v1.csv
    hourly/feed_items.csv
    wide/feed_items.csv
    authors/author_profiles.csv
    sources.json                        # provenance for key files
  timeseries/
    metadata/YYYY-MM-DD/*.csv           # non-empty metadata CSVs
    panel/panel_v1_YYYY-MM-DD.csv
    hourly/YYYY-MM-DD/HH/feed_items.csv # merged from parts/feed_items_part_*.csv
    wide/YYYY-MM-DD/feed_items.csv      # merged from parts/feed_items_part_*.csv
    authors/YYYY-MM-DD/author_profiles.csv
    micro5/STUDY_ID/SAMPLE_FAMILY/YYYY-MM-DD/HH/MM/
      feed_items.csv                    # merged from parts/feed_items_part_*.csv
      posts_first_seen.csv              # merged from parts/posts_first_seen_part_*.csv
      post_metrics.csv                  # merged from parts/post_metrics_part_*.csv
      post_labels.csv                   # merged from parts/post_labels_part_*.csv
```

## Current Operator Handoff (2026-03-20)

This section is intentionally date-stamped. It captures the important local context for the next agent/operator and may become stale over time.

- Active default collection path on this machine is the screen-managed daemon in frozen-study mode, preferring `micro10_full_live_20260319`.
- The local git/worktree state is **not clean**. Do not assume `git status` reflects a neat committed v2 transition:
  - the historical committed tree still points at the older `bsky_fair_collect` package
  - the active `bsky_collector_v2/`, `scripts/`, `tests/`, and this `README.md` currently live in a dirty worktree and may appear untracked relative to `HEAD`
  - future agents should inspect the working tree before trying to "restore" or "clean up" files
- Despite the legacy `micro5/.../micro5_core_full` naming, the preferred default study is a **10-minute** design:
  - fixed **1500-feed** frozen panel
  - **Top50** depth
  - `viewer_modes = unauth,auth`
  - `sample_family = micro5_core_full` only for historical naming compatibility
- Current analysis boundary for paper-grade work:
  - treat `2026-03-19T05:30:00Z` as the effective study start for the current fixed-panel design
  - data collected before `2026-03-19` should be treated as **temporarily unavailable / out of scope** for the main analysis, even if older files still exist on disk
  - the reason is not simple missingness: the pre-`2026-03-19` archive primarily reflects the older `hourly` collection regime, which is not directly comparable to the current fixed-panel study
  - do **not** pool pre-`2026-03-19` hourly outputs with post-`2026-03-19` frozen-study outputs unless a future analysis explicitly normalizes for the design change
- For current empirical work, prefer the post-`2026-03-19` frozen-study cohort only:
  - study id: `micro10_full_live_20260319`
  - fixed panel size: `1500`
  - realized panel composition: `700` `popular_by_likecount` + `800` `longtail_random`
  - cadence: one snapshot every `10` minutes
  - depth: `Top50`
  - viewer modes: `unauth` and `auth`
- As of handoff, the latest verified healthy completed window was `2026-03-20T07:20:00Z` to `2026-03-20T07:30:00Z` and it was `promoted`. Re-check live status before assuming this is still the latest window.
- The fixed panel used by `micro10_full_live_20260319` comes from panel version `2026-03-16` and its realized composition is:
  - `700` `popular_by_likecount`
  - `800` `longtail_random`
  - `0` `onboarding_surfaced`
  - `0` `suggested`
- Why the current panel has no onboarding bucket:
  - `starterpack_accounts.csv` is populated, but `starterpack_feeds.csv` has been empty across the current archive slice that was checked
  - logs repeatedly report `starterpack has no embedded feeds`
  - because onboarding feed rows are absent, `onboarding_surfaced` is effectively empty for the current panel build
- Why the current panel has no suggested bucket:
  - on the current frozen panel date, suggested candidates that existed were already absorbed into the top `popular_by_likecount` prefix after de-duplication, leaving no remaining rows for a separate suggested bucket
- Raw micro study windows live under:
  - `data_v2_full/micro5/<study_id>/<sample_family>/YYYY-MM-DD/HH/MM/`
- Analysis-friendly flattened micro exports now also live under:
  - `data_v2_full/effective_csv/timeseries/micro5/<study_id>/<sample_family>/YYYY-MM-DD/HH/MM/`
  - each exported window contains `feed_items.csv`, `posts_first_seen.csv`, `post_metrics.csv`, and `post_labels.csv`
- When operators asked to restore the "old storage format" for current collection, the agreed meaning was:
  - the **v2** flattened `effective_csv/timeseries/...` layout for easy downstream analysis
  - **not** the older v1 `data_run_*/02_csv_exports/*.csv.gz` archive-bundle format
- The collector now writes those flattened micro exports automatically after each completed `micro-snapshot-study` window. This is a storage/export change only; the collection logic itself was not changed.
- Historical backfill status at handoff:
  - all completed windows currently present for `micro10_full_live_20260319` were backfilled to the flattened `effective_csv/timeseries/micro5/...` path
  - verified count at handoff: `116/116` completed windows exported
- Most important recent operational trap:
  - a live `state-writer` PID can exist while the unix socket is gone or non-responsive
  - the daemon path was updated to treat the writer as healthy only when PID, socket, and a real RPC probe all succeed
  - if collection suddenly "starts" but windows produce no rows, check `collector-daemon.log` and the `state-writer` socket health first
- Current analysis outputs from this handoff live under:
  - `output/analysis_demo_20260319/`
  - `output/analysis_demo_20260320/`
- Most relevant analysis artifacts:
  - `output/analysis_demo_20260319/dced_gap_metrics_micro10_full_24h.json`
  - `output/analysis_demo_20260320/dced_trajectory_gap_metrics_micro10_full_24h_1h.json`
  - `output/analysis_demo_20260320/dced_gatekeeping_audit_micro10_full_24h_1h.json`
  - `output/analysis_demo_20260320/dced_trajectory_gap_metrics_micro10_full_24h_1h_availability_strict20m.json`
  - `output/analysis_demo_20260320/dced_trajectory_gap_metrics_micro10_full_24h_1h_availability_strict10m.json`
  - `output/analysis_demo_20260320/dced_trajectory_gap_metrics_micro10_full_24h_1h_availability_strict30m.json`
- Best current first-pass interpretation of the duplicate-conditioned exposure analysis:
  - duplicate definition is still provisional: `exact-text minus URL`, not the final `URL + hashtag + text-signature` design
  - timing explains some but not most residual exposure gap
  - simple author prestige variables (`followers`, `posts`) do not robustly explain the remaining residual gap
  - early trajectory explains a small additional slice, but still leaves most residual unexplained
  - for stricter early-trajectory work, use `availability_time = max(record_created_at, indexed_at)` and a first-monitor-delay cohort filter instead of treating all posts as equally well observed from birth
  - current best strict-cohort compromise is `strict20m`:
    - retains about `30%` of posts, `26%` of risk-set rows, and `25%` of exposure from the analyzable duplicate cohort
    - raises early-trajectory feature coverage to about `80%`
    - still shows author covariates adding little after timing, while early trajectory adds only a modest extra slice
  - `strict10m` is cleaner but much smaller; `strict30m` is larger but somewhat less clean
  - by average residual mass, `in-ranking residual` is larger than `gatekeeping residual`
  - by failure severity, the ugliest cases are still `single-winner gatekeeping`
  - `popular_by_likecount` looks worse than `longtail_random`
  - `auth` and `unauth` look broadly similar at this first pass
- If a future agent needs to rebuild flattened exports from raw outputs, use:
  - `python -m bsky_collector_v2 sync-effective-csv --out-base /Volumes/T9/BlueSky/data_v2_full`
  - note that this can be slow if run against the full archive
  - for one specific micro window, prefer the in-process automatic export or a targeted call to the effective export helpers instead of a full rebuild
- External-drive caveat:
  - this repo lives on `/Volumes/T9`, so AppleDouble sidecar files like `._*` may appear
  - broad scans such as `compileall` can report false `SyntaxError: source code string cannot contain null bytes` on those sidecar files
  - when that happens, inspect or remove the `._*` files rather than assuming the collector source itself is corrupt

## Setup

From repo root (`/Volumes/T9/BlueSky`):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

## Auth env file (optional)

Create a file like `auth.env`:

```bash
BSKY_IDENTIFIER=your-handle-or-email
BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
BSKY_PDS_HOST=https://bsky.social
```

Never commit this file.

## CLI

Entry point:

```bash
python -m bsky_collector_v2 <subcommand> [global flags] [subcommand flags]
```

Global flags:

- `--out-base /Volumes/T9/BlueSky/data_v2_full`
- `--env-path auth.env` (or set `BSKY_ENV_PATH`)
- `--log-level info|debug`
- `--appview-host https://public.api.bsky.app` (or set `BSKY_APPVIEW_HOST`)
- `--pds-host https://bsky.social` (or set `BSKY_PDS_HOST`)
- `--relay-host https://bsky.network` (or set `BSKY_RELAY_HOST`)
- `--rps 20`
- `--concurrency 16`
- `--posts-per-feed 50`
- `--time-budget-minutes 55`
- `--feed-time-budget-s 20`
- `--viewer-modes unauth,auth`
- `--accept-language en-US` (or set `BSKY_ACCEPT_LANGUAGE`)
- `--accept-labelers did:plc:...` (or set `BSKY_ACCEPT_LABELERS`)
- `--vantage-id-unauth unauth_enUS` (or set `BSKY_VANTAGE_ID_UNAUTH`)
- `--vantage-id-auth auth_enUS` (or set `BSKY_VANTAGE_ID_AUTH`)
- `--resume` / `--no-resume`
- `--dry-run`

Subcommands:

- `healthcheck`
- `refresh-discovery`
- `index-feed-generators` (resumable; uses `--relay-host` for `com.atproto.sync.*` repo enumeration, `--pds-host` for `com.atproto.repo.listRecords`)
- `build-panel`
- `snapshot-panel` (`--snapshot-hour-utc 2026-02-13T01:00:00Z` optional)
- `study-benchmark` (`--panel-path ...`, writes `studies/benchmarks/bench_<id>.json`)
- `study-init` (`--benchmark-path ... --sample-family micro5_core_full|micro5_extended_sharded`)
- `micro-snapshot-study` (`--study-id ... --scheduled-window-start-utc 2026-03-17T00:00:00Z`)
- `wide-sweep` (`--n-feeds 5000`)
- `hydrate-authors` (`--max-authors 50000`, `--batch-size 25`)
  - `--seen-after-utc YYYY-MM-DDTHH:MM:SSZ`: only hydrate authors first seen at/after this UTC timestamp
  - `--seen-before-utc YYYY-MM-DDTHH:MM:SSZ`: only hydrate authors first seen before this UTC timestamp
- `state-writer` (`--socket-path /tmp/bsky_state_writer.sock` or `--tcp 127.0.0.1:9911`)
- `sync-effective-csv` (rebuild `effective_csv/` from current outputs, including flattened `micro5` window exports)
- `backfill-run-artifacts` (retrofit legacy `data_v2_full` runs with enriched manifest fields, quality reports, and best-effort request provenance)
- `backfill-interactions` (optional; currently a no-op placeholder)

## Frozen Micro5 Study Mode

Use frozen micro5 study mode for paper-grade RQ1/RQ2 collection where panel membership and window timing must remain stable across days.

- `hourly` ecosystem monitoring is still the right tool for discovery refresh, daily panel construction, wide sweeps, and general observability.
- `micro5` study mode is a separate path with a frozen study panel, explicit 5-minute windows, request timestamps, deterministic per-window randomization, stricter quality gates, and no dependence on the mutable active `panel/panel_v1.csv`.

Important: changing cron from hourly to every 5 minutes is **not** sufficient for the main study. The default daemon still runs daily `refresh-discovery` and daily `build-panel`, so using the active panel without freezing it introduces cross-day feed-set drift.

### Study Workflow

1. Benchmark the current machine/config against the actual panel and requested viewer/depth settings.
2. Freeze a study-owned panel under `studies/<study_id>/panel/frozen_panel.csv`.
3. Run `micro-snapshot-study` on aligned 5-minute windows, or use the dedicated `collector_study_daemon.sh`.

### Benchmark

Benchmark the current panel with the intended viewer modes and depth:

```bash
python -m bsky_collector_v2 study-benchmark \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --panel-path /Volumes/T9/BlueSky/data_v2_full/panel/panel_v1.csv \
  --env-path /Volumes/T9/BlueSky/auth.env \
  --viewer-modes unauth,auth \
  --posts-per-feed 50 \
  --concurrency 16 \
  --rps 20 \
  --sample-size 200 \
  --safety-margin 0.85
```

This writes `studies/benchmarks/bench_<id>.json` and reports:

- observed snapshot request throughput on the current machine/config
- estimated full 5-minute sweep duration
- safe maximum panel size with headroom
- whether the requested full same-panel design fits
- how many deterministic shards are required if it does not fit

### Freeze A Core Study

Freeze a same-panel-every-window core study. `study-init` refuses impossible full-panel designs unless you explicitly ask it to trim to a benchmark-safe core size.

```bash
python -m bsky_collector_v2 study-init \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --benchmark-path /Volumes/T9/BlueSky/data_v2_full/studies/benchmarks/bench_<id>.json \
  --source-panel-path /Volumes/T9/BlueSky/data_v2_full/panel/panel_v1.csv \
  --sample-family micro5_core_full \
  --env-path /Volumes/T9/BlueSky/auth.env \
  --viewer-modes unauth,auth \
  --posts-per-feed 50 \
  --auto-core-size
```

Use `micro5_core_full` when the same frozen panel can truly fit inside the benchmarked 5-minute window with headroom.

### Freeze An Extended Sharded Study

Freeze a larger panel and rotate deterministic shards across windows when the full same-panel design does not fit:

```bash
python -m bsky_collector_v2 study-init \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --benchmark-path /Volumes/T9/BlueSky/data_v2_full/studies/benchmarks/bench_<id>.json \
  --source-panel-path /Volumes/T9/BlueSky/data_v2_full/panel/panel_v1.csv \
  --sample-family micro5_extended_sharded \
  --env-path /Volumes/T9/BlueSky/auth.env \
  --viewer-modes unauth,auth \
  --posts-per-feed 50 \
  --auto-shard-count
```

Use `micro5_extended_sharded` when the larger frozen panel cannot be collected in every 5-minute window. Keep this sample family analytically separate from the core panel.

### Run A Study Window

Run one explicit 5-minute window:

```bash
python -m bsky_collector_v2 micro-snapshot-study \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --env-path /Volumes/T9/BlueSky/auth.env \
  --study-id <study_id> \
  --scheduled-window-start-utc 2026-03-17T00:00:00Z \
  --rps 20 \
  --concurrency 16 \
  --feed-time-budget-s 20 \
  --resume
```

### Recommended 14-Day Study Runner

For a paper-grade frozen study loop, use the dedicated study daemon instead of the hourly daemon:

```bash
ROOT=/Volumes/T9/BlueSky \
OUT_BASE=/Volumes/T9/BlueSky/data_v2_full \
ENV_PATH=/Volumes/T9/BlueSky/auth.env \
STUDY_ID=<study_id> \
/Volumes/T9/BlueSky/scripts/collector_study_daemon.sh \
  --study-id <study_id> \
  --sample-family micro5_core_full
```

Recommended operational defaults:

- run the frozen core panel every 5 minutes if and only if the benchmark says it fits with headroom
- use a separate extended sharded study for the larger frozen panel when it does not fit
- keep `refresh-discovery`, `build-panel`, `wide-sweep`, and `hydrate-authors` off the critical micro5 loop, or run them as clearly separate auxiliary jobs
- treat `micro5_core_full` and `micro5_extended_sharded` as separate sample families in analysis

Migration notes:

- existing hourly / wide / metadata manifests and quality reports are unchanged
- micro5 writes to `micro5/<study_id>/<sample_family>/YYYY-MM-DD/HH/MM/` and never reuses the hourly layout
- request provenance now includes additional study/window fields used by micro5; legacy hourly outputs still read correctly

## Single-writer mode (shared control_state.db)

To avoid SQLite write-lock contention across concurrent jobs, run one dedicated state writer and point jobs at it:

Unix socket (macOS/Linux/WSL2):

```bash
# Terminal 1: start writer
python -m bsky_collector_v2 state-writer \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --socket-path /tmp/bsky_state_writer.sock

# Terminal 2+: run normal jobs through the writer
export BSKY_STATE_WRITER_SOCKET=/tmp/bsky_state_writer.sock
python -m bsky_collector_v2 snapshot-panel --out-base /Volumes/T9/BlueSky/data_v2_full ...
python -m bsky_collector_v2 wide-sweep --out-base /Volumes/T9/BlueSky/data_v2_full ...
python -m bsky_collector_v2 hydrate-authors --out-base /Volumes/T9/BlueSky/data_v2_full ...
python -m bsky_collector_v2 index-feed-generators --out-base /Volumes/T9/BlueSky/data_v2_full ...
```

TCP (works on native Windows too):

```bash
# Terminal 1: start writer
python -m bsky_collector_v2 state-writer \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --tcp 127.0.0.1:9911

# Terminal 2+: run normal jobs through the writer
export BSKY_STATE_WRITER_SOCKET=tcp://127.0.0.1:9911
python -m bsky_collector_v2 snapshot-panel --out-base /Volumes/T9/BlueSky/data_v2_full ...
python -m bsky_collector_v2 wide-sweep --out-base /Volumes/T9/BlueSky/data_v2_full ...
python -m bsky_collector_v2 hydrate-authors --out-base /Volumes/T9/BlueSky/data_v2_full ...
python -m bsky_collector_v2 index-feed-generators --out-base /Volumes/T9/BlueSky/data_v2_full ...
```

When `BSKY_STATE_WRITER_SOCKET` is set (either a unix socket path or a `tcp://HOST:PORT`), `ControlState` calls are proxied to the writer process and only the writer touches `control/control_state.db`.

## Unattended 2-3 week collection (auto-restart)

For this machine/path layout (`/Volumes/T9/...`), use the screen-managed daemon:

```bash
# start detached guardian (auto-restarts daemon if it exits)
/Volumes/T9/BlueSky/scripts/collector_screen_ctl.sh start

# optionally pin a specific frozen study instead of auto-discovering the latest core study
STUDY_ID=micro5_core_live_20260317 /Volumes/T9/BlueSky/scripts/collector_screen_ctl.sh restart

# check status
/Volumes/T9/BlueSky/scripts/collector_screen_ctl.sh status

# inspect wrapper + daemon logs
/Volumes/T9/BlueSky/scripts/collector_screen_ctl.sh logs

# attach to live session (optional)
/Volumes/T9/BlueSky/scripts/collector_screen_ctl.sh attach

# stop
/Volumes/T9/BlueSky/scripts/collector_screen_ctl.sh stop
```

Notes:

- `collector_screen_ctl.sh` runs `collector_daemon.sh` inside `screen` and wraps it in an infinite restart loop.
- `collector_daemon.sh` now defaults to **micro5 frozen-study mode**, prefers `micro10_full_live_20260319` when that study manifest exists, and otherwise falls back to the newest `micro5_core_full` study manifest under `data_v2_full/studies/`.
- The preferred default study `micro10_full_live_20260319` is the fixed **1500-feed / Top50 / 10-minute / unauth+auth** collection setup backed by a frozen panel and a benchmarked full-panel feasibility check.
- This provides process-level auto-restart without manual babysitting.
- If the machine reboots, restart with `collector_screen_ctl.sh start` (or set up an OS login item to run that command).
- `launchd/cron` jobs may fail with `Operation not permitted` when commands touch `/Volumes/T9/...` on some macOS setups.

If your `out-base` is in a user-home path (not restricted volume), you can also use the launchd controller:

```bash
/Volumes/T9/BlueSky/scripts/collector_daemon_ctl.sh start
/Volumes/T9/BlueSky/scripts/collector_daemon_ctl.sh status
/Volumes/T9/BlueSky/scripts/collector_daemon_ctl.sh logs
/Volumes/T9/BlueSky/scripts/collector_daemon_ctl.sh stop
```

Daemon behavior:

- Keeps `state-writer` alive (`/tmp/bsky_state_writer_prod.sock`)
- Auto-starts jobs with safe defaults and `--resume`
- Restarts jobs when they exit/crash
- Defaults to **full collection on each daemon start** (resets schedule stamps so all job types are attempted immediately once)
- Uses interval scheduling:
  - `micro-snapshot-study`: every 10 minutes in default `micro5` mode, using the preferred frozen study (`micro10_full_live_20260319`) when available
  - `index-feed-generators`: hourly
  - `hydrate-authors`: every 3 hours
  - `refresh-discovery`: daily
  - `wide-sweep`: daily
- After each completed `micro-snapshot-study` window, the daemon path also writes flattened exports under `effective_csv/timeseries/micro5/...`
- Keeps `build-panel` **off by default** in `micro5` mode so the active mutable panel does not overwrite the frozen study design.
- To return to the old hourly collector as the main path, start the daemon with `COLLECTOR_MODE=legacy_hourly`.

Main daemon log:

`data_v2_full/logs/launchd/collector-daemon.log`

Optional override:

- Set `FORCE_FULL_COLLECTION_ON_START=0` before starting daemon if you want to keep old schedule stamps on restart.
- Set `STUDY_ID=<study_id>` to pin a specific frozen study instead of auto-discovery.
- Set `DEFAULT_STUDY_ID=<study_id>` if you want a different preferred default frozen study.
- Set `ENABLE_BUILD_PANEL=1` only if you explicitly want the daemon to resume mutating the active panel.

## Throughput guardrails (to reduce upstream failures)

When running multiple collectors concurrently, keep defaults in the safe zone:

- Recommended: `--rps 20 --concurrency 16` for `snapshot-panel` and `wide-sweep`
- Acceptable (higher pressure): up to about `--rps 30 --concurrency 24`
- Avoid for all-jobs-parallel runs: `--rps 120 --concurrency 96` (observed much higher `wide-sweep` failure ratio with lower effective yield)

In timed stress runs (same workload window), `wide-sweep` failure ratio rose from about `4%` (`16/20`) to about `21%` (`24/30`) and about `52%` (`96/120`), with no SQLite lock errors (single-writer mode) and no HTTP `429` responses.

## Scheduling examples

The cron examples below are for the legacy hourly collector path. The default daemon is now `micro5` frozen-study mode; use `COLLECTOR_MODE=legacy_hourly` if you want these schedules to be your main collection loop.

### Cron (simple)

Hourly snapshots (unauth + auth when env available):

```cron
0 * * * * cd /Volumes/T9/BlueSky && .venv/bin/python -m bsky_collector_v2 snapshot-panel --out-base /Volumes/T9/BlueSky/data_v2_full --env-path /Volumes/T9/BlueSky/auth.env --accept-language en-US --vantage-id-unauth unauth_enUS --vantage-id-auth auth_enUS --viewer-modes unauth,auth --posts-per-feed 50 --concurrency 16 --rps 20 --time-budget-minutes 55 --feed-time-budget-s 20
```

Daily discovery + panel build:

```cron
15 0 * * * cd /Volumes/T9/BlueSky && .venv/bin/python -m bsky_collector_v2 refresh-discovery --out-base /Volumes/T9/BlueSky/data_v2_full --accept-language en-US --vantage-id-unauth unauth_enUS --concurrency 16 --rps 20
25 0 * * * cd /Volumes/T9/BlueSky && .venv/bin/python -m bsky_collector_v2 index-feed-generators --out-base /Volumes/T9/BlueSky/data_v2_full --env-path /Volumes/T9/BlueSky/auth.env --relay-host https://bsky.network --pds-host https://bsky.social --rps 20 --time-budget-minutes 55
30 1 * * * cd /Volumes/T9/BlueSky && .venv/bin/python -m bsky_collector_v2 build-panel --out-base /Volumes/T9/BlueSky/data_v2_full --concurrency 16 --rps 20
```

Daily wide sweep (unauth only, shallow):

```cron
0 2 * * * cd /Volumes/T9/BlueSky && .venv/bin/python -m bsky_collector_v2 wide-sweep --out-base /Volumes/T9/BlueSky/data_v2_full --accept-language en-US --vantage-id-unauth unauth_enUS --posts-per-feed 20 --n-feeds 10000 --concurrency 16 --rps 20 --feed-time-budget-s 20 --time-budget-minutes 55
```

Daily author hydration:

```cron
0 3 * * * cd /Volumes/T9/BlueSky && .venv/bin/python -m bsky_collector_v2 hydrate-authors --out-base /Volumes/T9/BlueSky/data_v2_full --accept-language en-US --vantage-id-unauth unauth_enUS --max-authors 50000 --batch-size 25 --concurrency 8 --rps 20
```

Strict same-time window mode (one hour):

```bash
SNAP_HOUR_UTC="$(date -u +%Y-%m-%dT%H:00:00Z)"
SNAP_NEXT_UTC="$(date -u -v+1H +%Y-%m-%dT%H:00:00Z)"

# 1) panel snapshots for this hour (unauth + auth)
.venv/bin/python -m bsky_collector_v2 snapshot-panel \
  --out-base /Volumes/T9/BlueSky/data_v2_full --env-path /Volumes/T9/BlueSky/auth.env \
  --accept-language en-US --vantage-id-unauth unauth_enUS --vantage-id-auth auth_enUS \
  --viewer-modes unauth,auth --posts-per-feed 50 --concurrency 16 --rps 20 \
  --snapshot-hour-utc "$SNAP_HOUR_UTC" --time-budget-minutes 600

# 2) wide sweep for the same hour window (optional for long-tail comparators)
.venv/bin/python -m bsky_collector_v2 wide-sweep \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --accept-language en-US --vantage-id-unauth unauth_enUS \
  --posts-per-feed 10 --n-feeds 10000 --concurrency 8 --rps 20 \
  --time-budget-minutes 600

# 3) hydrate only authors first seen in the same hour window
.venv/bin/python -m bsky_collector_v2 hydrate-authors \
  --out-base /Volumes/T9/BlueSky/data_v2_full --accept-language en-US --vantage-id-unauth unauth_enUS \
  --seen-after-utc "$SNAP_HOUR_UTC" --seen-before-utc "$SNAP_NEXT_UTC" \
  --max-authors 50000 --batch-size 25 --concurrency 8 --rps 20
```

### Window runner for 10h strict mode

To execute the strict same-time sequence automatically for multiple windows, use:

```bash
python /Volumes/T9/BlueSky/scripts/run_windowed_collection.py --hours 10
```

Useful options:

- `--start-hour-utc 2026-02-15T10:00:00Z` sets the first window start.
- `--skip-wide` disables wide-sweep for faster only-panel-only windows.
- `--sleep-seconds 120` pauses between windows.
- `--project-root`, `--out-base`, `--auth-path`, `--accept-language` override env.

### launchd (macOS sketch)

Use `StartCalendarInterval` for hourly `snapshot-panel` and daily `refresh-discovery` / `build-panel`. Keep `WorkingDirectory=/Volumes/T9/BlueSky` and run with `.venv/bin/python`.

### Strict Window Runner (legacy utility, not paper-grade study mode)

For fixed-minute starts and multi-window per-hour collection, use:

```bash
python /Volumes/T9/BlueSky/scripts/run_strict_window_collection.py \
  --hours 10 \
  --micro-sweep-minutes 0,20,40 \
  --sleep-until-window
```

Notes:

- This helper is **not** a substitute for frozen study mode. It still wraps ordinary `snapshot-panel` and does not freeze or validate a study panel.
- Multi-window snapshots default to minute-specific out-bases such as `..._m00`, `..._m20`, `..._m40` unless you pass `--micro-out-base-template`.
- `wide-sweep` and `hydrate-authors` still run once per base hour in this runner to avoid changing collector layout semantics.
- `--fixed-start-minute 0` is a shorthand for one fixed-minute window per hour.

### Windows Labelerexp Daemon

The experimental labelerexp arm also has Windows-specific daemon/controller scripts:

- `scripts/collector_daemon_labelerexp_windows.ps1`
- `scripts/collector_daemon_labelerexp_windows_ctl.ps1`

## Live monitoring (“watch”)

Global logs:

```bash
tail -f /Volumes/T9/BlueSky/data_v2_full/logs/collector.log
tail -f /Volumes/T9/BlueSky/data_v2_full/logs/errors.log
```

Per-hour snapshot logs + progress:

```bash
tail -f /Volumes/T9/BlueSky/data_v2_full/hourly/YYYY-MM-DD/HH/logs/snapshot.log
cat /Volumes/T9/BlueSky/data_v2_full/hourly/YYYY-MM-DD/HH/progress.json
ls -lh /Volumes/T9/BlueSky/data_v2_full/hourly/YYYY-MM-DD/HH/parts/
```

Wide sweep logs + progress:

```bash
tail -f /Volumes/T9/BlueSky/data_v2_full/wide/YYYY-MM-DD/logs/wide.log
cat /Volumes/T9/BlueSky/data_v2_full/wide/YYYY-MM-DD/progress.json
ls -lh /Volumes/T9/BlueSky/data_v2_full/wide/YYYY-MM-DD/parts/
```

## Notes on “popular feeds”

The official “popular feed generators” endpoint may return only ~100–200 feeds. Panel “popular” is defined as the **top feeds by `likeCount` across the discovered feed catalog**, not by requiring 800+ results from the popular endpoint.

## Notes on feed generator indexing

`index-feed-generators` first attempts `com.atproto.sync.listReposByCollection` on `--relay-host`. If that endpoint is unsupported/flaky (e.g. `400 failed to proxy`), it falls back to scanning `com.atproto.sync.listRepos` and probing each repo with `com.atproto.repo.listRecords` to find `app.bsky.feed.generator` records. This is slower, but still fully resumable via `control/control_state.db`.

## Tests

```bash
make test
```
