# RQ Collection Handoff 2026-04-02

## Purpose

This repo is organized around two research questions:

- `RQ1`: conditional exposure disparity among semantically same or near-duplicate posts in monitored top-K feed panels
- `RQ2`: frame exposure vs frame supply disparity inside topic/event clusters

It is not just a generic Bluesky crawler. Collector, backfill, and workflow decisions should be judged against those two questions.

## Canonical Facts

- Canonical data root: `data_v2_full/`
- Preserve the existing output layout: `hourly/`, `wide/`, `micro5/`, `interactions/`, `metadata/`, `effective_csv/`
- Do not delete historical raw data
- Prefer `state-writer` / `BSKY_STATE_WRITER_SOCKET` whenever multiple jobs may touch `control/control_state.db`

## Current Collector Modes

### 1. Paper-grade fixed-panel mainline

Use this for the main study design.

- wrappers: `scripts/collector_daemon.sh`, `scripts/collector_study_daemon.sh`, `scripts/collector_screen_ctl.sh`
- default study: `micro10_full_live_20260319`
- design: fixed 1500-feed panel, Top50, 10-minute cadence
- viewer modes: `unauth` + `auth`

### 2. Public-only omnivore / backfill mode

Use this for public discovery and public-state hydration/backfill.

- wrapper: `scripts/collector_public_omnivore_daemon.sh`
- job entry: `python -m bsky_collector_v2 collect-public-omnibus`
- viewer mode is forced to `unauth`

This mode is useful, but it is not a drop-in replacement for the auth+unauth fixed-panel study.

### 3. Unified operator wrapper

Use this when you want the cleanest operator-facing entrypoint:

- `scripts/collector_rq_daemon.sh fixed-panel`
- `scripts/collector_rq_daemon.sh realtime-backfill`
- `scripts/collector_rq_daemon.sh history-backfill`
- `scripts/collector_rq_daemon.sh full-stack`

This wrapper preserves the older commands while exposing clearer profiles.

## Recommended Startup Commands

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
- discovery/panel/snapshot/wide/hydrate/micro loops disabled by default
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

Default behavior in that profile:

1. `seed-post-registry`
2. `backfill-interactions`
3. `backfill-rq1-factors`

It uses one shared state writer and keeps the old raw layout untouched.

### Full research stack

```bash
./scripts/collector_rq_daemon.sh full-stack
```

This starts:

1. the fixed-panel daemon
2. waits for its state writer
3. starts the realtime public backfill loop against that same writer

## RQ2 Workflow

Canonical CLI:

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

Stages:

1. topic probing / topic batch
2. event clustering
3. annotation candidate sampling
4. cluster label application when `*_cluster_labels*.csv` is present
5. annotation merge when `*_annotations.csv` is present
6. basic `frame_tables/` generation for exposure vs supply

Current `frame_tables/` outputs are intentionally basic, not a final causal design artifact.

## What Changed In This Round

### Audited the working tree against the provided 2026-04-02 zip baseline

The provided zip was treated as the source-of-truth audit baseline. Existing second-round working-tree additions were kept only where they remained consistent with that baseline and with the requested collector/RQ2 goals.

### Kept the unified launcher as the recommended operator entrypoint

Primary wrapper in the working tree:

- `scripts/collector_rq_daemon.sh`

Profiles:

- `fixed-panel`
- `realtime-backfill`
- `history-backfill`
- `full-stack`

The wrapper and the underlying Bash daemons now auto-detect a working repo-local interpreter from `.venv/bin/python` or `.venv-win/Scripts/python.exe`, while still allowing an explicit `PYTHON_BIN` override.

### Validated wrapper-managed shared state across unix/TCP targets

The wrapper path was validated against:

- unix socket paths such as `/tmp/bsky_state_writer_prod.sock`
- `tcp://HOST:PORT` targets

This keeps the existing Unix path while making wrapper-managed shared state usable in cross-shell/native-Windows setups.

During validation, `history-backfill` exposed a real shared-state bug: `seed_post_registry.py` still touched `control.conn` directly for summary counts. That path is now remote-safe via explicit `ControlState` count methods, and a TCP `state-writer` regression test was added.

### Promoted the existing RQ2 work into a first-class documented workflow

Preserved, wired, and validated:

- `bsky_collector_v2/rq2_pipeline.py`
- `bsky_collector_v2/rq2_frame_tables.py`
- `scripts/run_rq2_pipeline.py`
- `python -m bsky_collector_v2 rq2-pipeline`
- `python -m bsky_collector_v2 rq2-generate-frame-tables`
- `tests/test_rq2_frame_tables.py`
- `tests/test_cli_rq2_pipeline.py`

What these add:

- one canonical RQ2 pipeline command in the main CLI
- one canonical CLI for the final basic frame-table materialization step
- automatic topic batch + cluster sampling orchestration
- optional label-application / annotation-merge stages
- basic frame exposure vs supply tables

### Rewrote the root agent guide and aligned README

- `agent.md` now reflects the actual research goals, canonical paths, honest RQ1/RQ2 status, and recommended launch commands
- `README.md` was updated to match the current collector/backfill/RQ2 entrypoints

## What I Did Not Change

- did not rename `data_v2_full`
- did not remove historical data
- did not collapse public-only collection into the paper-grade auth+unauth study path
- did not claim RQ2 is fully productionized
- did not replace the underlying collector architecture; the second-round changes sit on top of the restored refactor

## RQ1 Readiness Assessment

Honest status:

- close to answerable in a serious monitored observational sense
- not complete

Why close:

- top-K appearance + rank
- timing/window/feed/bucket/viewer/vantage context
- author hydration
- interaction backfill
- richer RQ1 factor backfill
- duplicate/DCED analysis tooling

Why still imperfect:

- multimodal near-duplicates are not fully hardened
- public-only collection cannot capture private viewer state
- historical backfill reflects current public state, not true historical snapshots
- graph remains graph-lite rather than a timestamped graph history

Safe wording:

> enough to support a serious monitored observational RQ1, but not enough to claim that every driver of exposure disparity is observed

## RQ2 Readiness Assessment

Honest status:

- the collection base exists
- the workflow is now first-class enough to run end-to-end
- the final table stage is basic and intentionally conservative

Still missing:

- manual frame labeling is still part of the workflow
- declared-objective adjustment is not standardized into a definitive final output table
- viewer-mode and moderation comparisons still depend on which collection design actually captured those conditions

Safe wording:

> RQ2 collection base exists, and the topic/frame pipeline is now organized as a first-class workflow, but it is not yet fully productionized

## Validation Performed

### Test regressions

- zip baseline: `pytest -q` in the extracted zip tree
- working tree before final fixes: `pytest -q`
- working tree after final fixes: `pytest -q`

### Focused command and wrapper checks

- `python -m bsky_collector_v2 --help`
- `python -m bsky_collector_v2 seed-post-registry --help`
- `python -m bsky_collector_v2 collect-public-omnibus --help`
- `python -m bsky_collector_v2 rq2-pipeline --help`
- `python -m bsky_collector_v2 rq2-generate-frame-tables --help`
- `python scripts/run_rq2_pipeline.py --help`
- Git Bash `bash -n scripts/collector_daemon.sh`
- Git Bash `bash -n scripts/collector_public_omnivore_daemon.sh`
- Git Bash `bash -n scripts/collector_study_daemon.sh`
- Git Bash `bash -n scripts/collector_rq_daemon.sh`
- `scripts/collector_rq_daemon.sh help`
- one-shot dry-run sanity for `scripts/collector_public_omnivore_daemon.sh`
- one-shot sanity for `scripts/collector_rq_daemon.sh history-backfill` using `tcp://127.0.0.1:9914`
- one-shot dry-run sanity for `scripts/collector_rq_daemon.sh realtime-backfill` using `tcp://127.0.0.1:9915`
- one-shot dry-run sanity for `scripts/collector_rq_daemon.sh realtime-backfill` without `PYTHON_BIN`, verifying interpreter auto-detection via `tcp://127.0.0.1:9916`

## Known Limitations

- `collector_rq_daemon.sh` is an operator wrapper, not a replacement for the underlying daemons
- `full-stack` still depends on the fixed-panel daemon exposing a healthy state writer before the backfill loop starts
- `frame_tables/` is a monitored exposure/supply summary layer, not a finished causal adjustment layer
- public-only omnivore should still be described as public-only
- on this Windows host, shell validation required Git Bash because the `bash` alias pointed at WSL without an installed distro
