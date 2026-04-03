# Bluesky Collector Repo Guide

## Mission

This repo exists to support two research questions, not a generic crawler:

- `RQ1`: within monitored Bluesky/custom-feed top-K panels, after controlling timing and panel context, do semantically same or near-duplicate posts receive systematically different observed exposure opportunity, and which observable post/author/feed/viewer factors correlate with the residual?
- `RQ2`: within political topic/event clusters, does monitored top-K exposure track observed frame supply, and how does any residual frame disparity vary by feed type, viewer mode, and moderation environment?

Keep collector, backfill, and workflow decisions aligned to those two questions.

## Canonical Paths

- Repo root: this directory
- Canonical data root: `data_v2_full/`
- Do not rename `data_v2_full`
- Do not delete historical raw data
- Do not break the existing output layout: `hourly/`, `wide/`, `micro5/`, `interactions/`, `metadata/`, `effective_csv/`

## Live Status

As of `2026-04-02T03:32-04:00` on this Windows host:

- fixed-panel collector is running via `scripts/collector_daemon.sh` with active wrapper PID `29180`
- the current `state-writer` listener is active on `tcp://127.0.0.1:9911`; use that instead of the stale unix-socket metadata from older runs
- `micro-snapshot-study` for `micro10_full_live_20260319` is currently active against `data_v2_full/`
- `backfill-rq1-factors` is currently running
- a direct Windows backfill loop is running under PID `89200`; each cycle runs `backfill-interactions` then `backfill-rq1-factors` against `BSKY_STATE_WRITER_SOCKET=127.0.0.1:9911`
- `seed-post-registry` was launched on `2026-04-02T07:24:58Z`; see `data_v2_full/control/seed_post_registry_last_run.json`
- on this host, `collector_public_omnivore_daemon.sh` mixed `python3` fallback and stale remote-state assumptions, so the stable live path is: fixed-panel collector daemon + current `state-writer` + direct backfill workers

## Collector Modes

### Paper-grade fixed-panel mainline

This is the current main study path:

- wrappers: `scripts/collector_daemon.sh`, `scripts/collector_study_daemon.sh`, `scripts/collector_screen_ctl.sh`
- default study: `micro10_full_live_20260319`
- design: fixed 1500-feed panel, Top50, 10-minute cadence
- viewer modes: `unauth` + `auth`

Use this when talking about the current paper-grade collection design.

### Public-only omnivore

This is a separate public-only path:

- wrapper: `scripts/collector_public_omnivore_daemon.sh`
- job entry: `python -m bsky_collector_v2 collect-public-omnibus`
- viewer mode is forced to `unauth`
- good for public discovery, hydration, public-state backfill, and optional public-only micro windows

Do not describe public-only omnivore as equivalent to the auth+unauth fixed-panel study.

### Unified Operator Wrapper

Preferred operator-facing wrapper:

- `scripts/collector_rq_daemon.sh fixed-panel`
- `scripts/collector_rq_daemon.sh realtime-backfill`
- `scripts/collector_rq_daemon.sh history-backfill`
- `scripts/collector_rq_daemon.sh full-stack`

This wrapper preserves the older commands while exposing clearer profiles.

The Bash wrappers auto-detect a working repo-local interpreter from `.venv/bin/python` or `.venv-win/Scripts/python.exe`. Override with `PYTHON_BIN` if your environment needs something else.

## Recommended Start Commands

### Fixed-panel collection

Recommended:

```bash
./scripts/collector_rq_daemon.sh fixed-panel
```

Compatible legacy command:

```bash
ROOT=/Volumes/T9/BlueSky \
OUT_BASE=/Volumes/T9/BlueSky/data_v2_full \
ENV_PATH=/Volumes/T9/BlueSky/auth.env \
DEFAULT_STUDY_ID=micro10_full_live_20260319 \
STUDY_ID=micro10_full_live_20260319 \
/Volumes/T9/BlueSky/scripts/collector_daemon.sh
```

### Realtime backfill

Recommended:

```bash
./scripts/collector_rq_daemon.sh realtime-backfill
```

Default behavior in that profile:

- `seed-post-registry`
- `backfill-interactions`
- `backfill-rq1-factors`
- no discovery/panel/snapshot/wide/hydrate/micro loops
- shared `state-writer`

Compatible legacy command:

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

### History backfill

Recommended:

```bash
./scripts/collector_rq_daemon.sh history-backfill
```

Compatible legacy sequence:

```bash
.venv/bin/python -m bsky_collector_v2 seed-post-registry --out-base /Volumes/T9/BlueSky/data_v2_full --include-hourly --include-wide --include-micro5 --include-posts-first-seen
.venv/bin/python -m bsky_collector_v2 backfill-interactions --out-base /Volumes/T9/BlueSky/data_v2_full --seen-before-utc 2026-03-19T05:30:00Z --max-posts 200000 --batch-size 25 --max-items-per-endpoint 0
.venv/bin/python -m bsky_collector_v2 backfill-rq1-factors --out-base /Volumes/T9/BlueSky/data_v2_full --seen-before-utc 2026-03-19T05:30:00Z --max-posts 200000 --batch-size 25 --max-items-per-endpoint 0 --max-thread-depth 1000 --max-thread-parent-height 1000
```

### Full research stack

```bash
./scripts/collector_rq_daemon.sh full-stack
```

This starts the fixed-panel daemon, waits for its shared state writer, and then starts the realtime public backfill loop against that same writer.

## State Writer Rule

If multiple jobs may touch `control/control_state.db`, prefer `state-writer` through `BSKY_STATE_WRITER_SOCKET`.

- unix socket targets still work
- `tcp://HOST:PORT` also works and is useful on native Windows or cross-shell setups

Do not add rough concurrent SQLite writes when a shared writer is available.

## RQ1 Readiness

Be honest:

- `RQ1` is close to answerable in a serious monitored observational sense
- the repo now has top-K appearance/rank, timing/window/feed/bucket/viewer/vantage context, author hydration, interaction backfill, richer RQ1 factor backfill, and duplicate/DCED analysis scripts

Still incomplete:

- multimodal near-duplicates such as same text with different image/context are not fully hardened
- public-only collection cannot recover private viewer preferences, home-timeline effects, or private moderation state
- historical backfill reflects current public state, not perfect historical-time snapshots
- graph is still graph-lite rather than a full timestamped graph history

Do not claim the repo already observes every driver of exposure disparity.

## RQ2 Readiness

Be honest:

- the collection base exists: `topic_probe`, `topic_batch`, `content_bias`, `annotation_sampling`, `cluster_label_apply`, `annotation_merge`
- there is now a first-class workflow through `python -m bsky_collector_v2 rq2-pipeline` or `python scripts/run_rq2_pipeline.py`
- the workflow can produce basic frame exposure vs supply tables

Still incomplete:

- frame labeling still depends partly on manual annotation
- declared-objective adjustment is not yet standardized into a definitive final analysis table
- viewer-mode and moderation comparisons still depend on which collection design actually captured those conditions

Do not claim RQ2 is fully productionized.

## RQ2 Workflow

Recommended command:

```bash
python -m bsky_collector_v2 rq2-pipeline \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --out-dir /Volumes/T9/BlueSky/output/rq2_pipeline \
  --annotation-dir /Volumes/T9/BlueSky/output/rq2_pipeline/annotations \
  --preset politics_v1
```

Compatibility wrapper:

```bash
python scripts/run_rq2_pipeline.py \
  --data-root /Volumes/T9/BlueSky/data_v2_full \
  --out-dir /Volumes/T9/BlueSky/output/rq2_pipeline \
  --annotation-dir /Volumes/T9/BlueSky/output/rq2_pipeline/annotations \
  --preset politics_v1
```

The workflow covers:

1. topic probing / topic batch
2. event clustering
3. annotation candidate sampling
4. cluster label application when `*_cluster_labels*.csv` exists
5. annotation merge when `*_annotations.csv` exists
6. basic `frame_tables/` generation for frame exposure vs supply

The current `frame_tables/` layer is intentionally basic. It is not a final causal adjustment artifact.

## Do Not

- do not delete historical data
- do not rename `data_v2_full`
- do not present the public-only collector as equivalent to the auth+unauth paper-grade study
- do not exaggerate RQ2 completion
- do not throw away the existing collector/backfill structure just to make the tree look cleaner
