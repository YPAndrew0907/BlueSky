# agent.md

This repository is a Bluesky ranking-observation / exposure-measurement collector for two research questions.

The collector is not just a generic scraper. Future agents must preserve the empirical design.

## Mission

The project is trying to answer two questions defined in the modeling deck:

- RQ1: within monitored top-K Bluesky / custom-feed panels, after controlling for timing and panel context, do semantically equivalent or near-duplicate posts receive unequal observed exposure opportunities, and which observable post / author / feed / viewer factors explain the residual gap?
- RQ2: within political topics or event clusters, do monitored top-K panels allocate observed exposure across frames in proportion to observed frame supply, and how does any residual disparity vary across feed type, viewer mode, or moderation environment?

## Canonical repository rules

1. Do not delete or rename `data_v2_full/`. Treat it as the canonical data root.
2. Do not delete historical raw data just to make the repo look cleaner.
3. Do not silently collapse `hourly`, `wide`, `micro5`, `interactions`, or `metadata` into a different on-disk schema unless you also ship a migration and compatibility layer.
4. Prefer additive refactors over destructive ones.
5. Preserve resumability and incremental write guarantees.
6. If multiple jobs may write `control/control_state.db` concurrently, prefer `BSKY_STATE_WRITER_SOCKET` / `state-writer` rather than direct concurrent SQLite writes.

## Current code-level source of truth

These files are the most important current code paths:

- `bsky_collector_v2/jobs/seed_post_registry.py`
- `bsky_collector_v2/jobs/public_omnibus.py`
- `bsky_collector_v2/jobs/backfill_interactions.py`
- `bsky_collector_v2/jobs/backfill_rq1_factors.py`
- `bsky_collector_v2/jobs/micro_snapshot_study.py`
- `bsky_collector_v2/state.py`
- `bsky_collector_v2/cli.py`
- `scripts/collector_daemon.sh`
- `scripts/collector_study_daemon.sh`
- `scripts/collector_public_omnivore_daemon.sh`

## What has already been refactored

The 2026-04-02 refactor already introduced the following:

1. A public-only omnibus collector entrypoint:
   - `python -m bsky_collector_v2 collect-public-omnibus`
   - implemented in `bsky_collector_v2/jobs/public_omnibus.py`
   - public-only, forces `viewer_modes=unauth`

2. Historical CSV seeding into `post_registry`:
   - `python -m bsky_collector_v2 seed-post-registry`
   - scans `hourly/`, `wide/`, `micro5/`, and optionally `posts_first_seen_part_*.csv`
   - allows older posts to enter the registry and be backfilled later

3. Stateful RQ1 factor backfill:
   - `post_rq1_factor_registry` support was added in `state.py`
   - `backfill-rq1-factors` now has queue / hydrated semantics similar to interaction backfill

4. `micro-snapshot-study --public-only`
   - allows a study window to be forced into unauth-only public mode

5. New test coverage for the new collector paths:
   - `tests/test_seed_post_registry.py`
   - `tests/test_public_omnibus.py`
   - `tests/test_cli_public_collectors.py`
   - strengthened `tests/test_rq1_factor_backfill.py`

## Current operational truth

There are now two important operational modes:

### A. Paper-grade fixed-panel study mode (auth + unauth capable)

Use this for the current main 10-minute frozen panel study.

Canonical wrappers:

- `scripts/collector_daemon.sh`
- `scripts/collector_study_daemon.sh`
- `scripts/collector_screen_ctl.sh`

Important current assumptions from the repo docs:

- preferred study id: `micro10_full_live_20260319`
- intended design: fixed 1500-feed panel, Top50, 10-minute cadence
- intended viewer comparison: `unauth` and `auth`
- this is the main analysis cohort for serious RQ1 / RQ2 work

### B. Public-only omnivore mode

Use this for public-only discovery + hydration + backfill + optional public-only study windows.

Canonical wrapper:

- `scripts/collector_public_omnivore_daemon.sh`

This mode is useful, but it does **not** fully replace the auth+unauth fixed-panel study design.

## Current launch commands (as-is, before any further refactor)

### 1. Start the current 10-minute fixed feed panel collection

Direct:

```bash
ROOT=/Volumes/T9/BlueSky \
OUT_BASE=/Volumes/T9/BlueSky/data_v2_full \
ENV_PATH=/Volumes/T9/BlueSky/auth.env \
DEFAULT_STUDY_ID=micro10_full_live_20260319 \
STUDY_ID=micro10_full_live_20260319 \
/Volumes/T9/BlueSky/scripts/collector_daemon.sh
```

Screen-managed:

```bash
cd /Volumes/T9/BlueSky
DEFAULT_STUDY_ID=micro10_full_live_20260319 \
STUDY_ID=micro10_full_live_20260319 \
./scripts/collector_screen_ctl.sh start
```

### 2. Start realtime backfill against newly observed posts

If the fixed-panel daemon is already running, it will also start a state-writer on `/tmp/bsky_state_writer_prod.sock`.
Use that socket to avoid SQLite lock fights.

```bash
cd /Volumes/T9/BlueSky
export BSKY_STATE_WRITER_SOCKET=/tmp/bsky_state_writer_prod.sock
ROOT=/Volumes/T9/BlueSky \
OUT_BASE=/Volumes/T9/BlueSky/data_v2_full \
SEED_REGISTRY=1 \
RUN_INDEX_FEED_GENERATORS=0 \
RUN_REFRESH_DISCOVERY=0 \
RUN_BUILD_PANEL=0 \
RUN_SNAPSHOT_PANEL=0 \
RUN_WIDE_SWEEP=0 \
RUN_HYDRATE_AUTHORS=0 \
RUN_HYDRATE_FEED_GENERATORS=0 \
RUN_MICRO_STUDIES=0 \
RUN_BACKFILL_INTERACTIONS=1 \
RUN_BACKFILL_RQ1_FACTORS=1 \
INTERVAL_PUBLIC_OMNIBUS_S=300 \
./scripts/collector_public_omnivore_daemon.sh
```

This is the current practical way to keep backfill near-realtime without editing the existing fixed-panel daemon.

### 3. Run history backfill for older posts already present in archived CSVs

First seed registry entries from historical CSVs:

```bash
cd /Volumes/T9/BlueSky
export BSKY_STATE_WRITER_SOCKET=/tmp/bsky_state_writer_prod.sock
.venv/bin/python -m bsky_collector_v2 seed-post-registry \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --include-hourly --include-wide --include-micro5 --include-posts-first-seen
```

Then run interaction backfill on the historical slice you want:

```bash
.venv/bin/python -m bsky_collector_v2 backfill-interactions \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --seen-before-utc 2026-03-19T05:30:00Z \
  --max-posts 200000 \
  --batch-size 25 \
  --max-items-per-endpoint 0
```

Then run RQ1 factor backfill on the same historical slice:

```bash
.venv/bin/python -m bsky_collector_v2 backfill-rq1-factors \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --seen-before-utc 2026-03-19T05:30:00Z \
  --max-posts 200000 \
  --batch-size 25 \
  --max-items-per-endpoint 0 \
  --max-thread-depth 1000 \
  --max-thread-parent-height 1000
```

If the historical queue is huge, shard it with `--shard-index` and `--shard-count`.

## Honest RQ status

### RQ1

The repo is now strong enough to support a serious monitored observational RQ1 workflow:

- top-K feed appearances and rank are collected
- timing and window context are collected
- feed / bucket / viewer mode / vantage are collected
- author metadata is hydratable
- interaction propagation is hydratable
- RQ1 factor backfill collects richer post / thread / actor / relationship context
- duplicate / cluster analysis scripts already exist

But RQ1 is still not literally “perfect”:

- multimodal near-duplicate detection is not fully solved (`same text, different picture/context` is only partially captured)
- public-only mode cannot answer viewer-private or home-timeline questions
- historical backfill gives current public state, not a true time machine
- graph collection is still graph-lite rather than full timestamped graph history

### RQ2

The repo has meaningful RQ2 building blocks:

- topic probing
- topic batching
- event clustering
- annotation candidate sampling
- cluster label application
- annotation merge utilities

But RQ2 is less complete than RQ1:

- there is not yet one canonical end-to-end production pipeline from collection -> topic/event clustering -> frame labels -> exposure/supply disparity tables
- frame labeling is still partly manual / annotation-driven
- declared-objective adjustments are not yet wired into a single standardized RQ2 output dataset
- viewer-mode / moderation-environment comparisons are only as good as the chosen collection mode

So future agents must not claim the current repo “perfectly answers” RQ2. The correct claim is that the collector is close on collection primitives, but the frame-analysis pipeline still needs integration and hardening.

## Highest-value remaining work

1. Unify the auth+unauth fixed-panel daemon and the realtime backfill path.
   - Either extend `scripts/collector_daemon.sh` or create a new wrapper that can run:
     - 10-minute fixed panel windows
     - seed-post-registry
     - realtime `backfill-interactions`
     - realtime `backfill-rq1-factors`
   - This should use the state-writer path by default.

2. Promote the RQ2 topic/frame tools into a first-class CLI workflow.
   - Add canonical subcommands (or wrapper scripts) for:
     - topic batch / topic probe
     - annotation sampling
     - cluster-label apply
     - annotation merge
     - final frame exposure vs frame supply table generation

3. Keep raw-data compatibility.
   - Do not break existing `hourly/`, `wide/`, `micro5/`, `interactions/` layouts.
   - If you add normalized outputs, put them under `effective_csv/` or `analysis/`.

4. Keep tests comprehensive.
   - Any daemon unification must include tests for schedule selection, state writer usage, and coexistence of micro study + backfill.

## Test expectations before shipping

At minimum, run:

```bash
pytest -q
```

And if you touch the new collection stack, specifically run:

```bash
pytest -q \
  tests/test_seed_post_registry.py \
  tests/test_public_omnibus.py \
  tests/test_cli_public_collectors.py \
  tests/test_rq1_factor_backfill.py \
  tests/test_micro_snapshot_study.py \
  tests/test_state.py
```

## Do not do these things

- do not delete `data_v2_full/`
- do not rename `micro5/` just because the current study is 10-minute
- do not pretend public-only collection equals auth+unauth collection
- do not claim RQ2 is complete unless you integrate the frame pipeline end to end
- do not silently change output schemas without docs and tests

## If you need one sentence to orient yourself

This repo’s main job is to observe monitored Bluesky feed exposure in a reproducible way, keep the fixed-panel study analytically stable, and backfill enough public context to support RQ1 now and a more complete RQ2 soon.
