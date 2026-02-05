# bsky_fair_collect

Read-only, resumable data collection pipeline for a cross-sectional snapshot of Bluesky’s open feed ecosystem (feed generators + starter packs + popular feeds + one snapshot per feed).

## Quick start

1) Create a run output directory (example):

```bash
mkdir -p /tmp/bsky_fair_run
```

2) Run the full pipeline:

```bash
cd bsky_fair_collect
python3 -m bsky_fair_collect run-all --out-dir /tmp/bsky_fair_run --auth-mode unauth
```

If you want authenticated viewer context (optional):

```bash
python3 -m bsky_fair_collect run-all --out-dir /tmp/bsky_fair_run --auth-mode both
```

## Credentials

This project loads credentials from:

`/Users/yipengandrewwang/BlueSky/.env.local`

Expected variables (preferred):
- `BSKY_HANDLE`
- `BSKY_APP_PASSWORD`
- `BSKY_PDS` (default: `https://bsky.social`)
- `BSKY_ACCESS_JWT` (optional)
- `BSKY_REFRESH_JWT` (optional; preferred if you already have it)

Compatibility aliases (if your `.env.local` uses these instead):
- `BLUESKY_HANDLE` -> `BSKY_HANDLE`
- `BLUESKY_IDENTIFIER` -> `BSKY_HANDLE`
- `BLUESKY_APP_PASSWORD` -> `BSKY_APP_PASSWORD`
- `BLUESKY_PDS` -> `BSKY_PDS`
- `BLUESKY_ACCESS_JWT` -> `BSKY_ACCESS_JWT`
- `BLUESKY_REFRESH_JWT` -> `BSKY_REFRESH_JWT`

## Outputs

Final analysis-ready outputs are flattened CSVs under:
- `OUT_DIR/csv/` (schemas are stable)

Operational artifacts:
- `OUT_DIR/logs/run.log` (human-readable log)
- `OUT_DIR/state/state.db` (resumable state; safe to delete only if restarting the run)

## Post-run backfill (recommended)

Some discovery/popular feeds may not be covered by the relay-index scan early in a run. To ensure
`service_did`/`provider_bucket`/`display_name` are filled for *all touched feeds* (starterpacks/popular/panel),
run:

```bash
cd bsky_fair_collect
python3 -m bsky_fair_collect backfill --out-dir OUT_DIR
```

## Post-run postprocess (optional convenience joins)

To create analysis-friendly joined tables + small derived metric tables (without changing `OUT_DIR/csv/`), run:

```bash
cd bsky_fair_collect
python3 -m bsky_fair_collect postprocess --out-dir OUT_DIR --zip
```

Outputs:
- `OUT_DIR/postprocess/feeds_flat.csv`
- `OUT_DIR/postprocess/impressions_flat.csv.gz`
- `OUT_DIR/postprocess/h1_discovery_concentration.csv`
- `OUT_DIR/postprocess/h2_provider_leverage.csv`
- `OUT_DIR/postprocess/h3_feed_exposure_concentration.csv`
- `OUT_DIR/postprocess/h4_feed_overlap_summary.csv`
- `OUT_DIR/postprocess/h5_exposure_vs_author_size.csv`
- `OUT_DIR/postprocess/h6_feed_label_risk.csv`
- `OUT_DIR/postprocess/h6_label_value_counts.csv`
- `OUT_DIR/postprocess/postprocess.zip` (optional)

Optional extras:
- Add `--labels-flat` to also write `OUT_DIR/postprocess/impression_labels_flat.csv.gz`
- Add `--no-metrics` to skip H1–H6 derived tables

## Run post-run steps automatically when the collector finishes

If you want a "wait until complete, then backfill + postprocess" helper:

```bash
python3 bsky_fair_collect/scripts/after_run.py \
  --out-dir OUT_DIR \
  --backfill auto \
  --postprocess \
  --postprocess-zip
```

## Notes

- Read-only only: this code never calls write endpoints (and explicitly avoids interaction endpoints).
- Cross-sectional only: one snapshot per feed per viewer mode.
- Starter packs: the collector enumerates starter pack creators via the relay (`app.bsky.graph.starterpack`), then uses AppView `app.bsky.graph.getActorStarterPacks` + `app.bsky.graph.getStarterPack` to hydrate feed URIs.
