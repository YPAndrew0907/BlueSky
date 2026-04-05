# Bluesky Fair Collect — Run Receipt (Slide2)

## Run identity (UTC)

- **run_id:** `4db1ff38a86a452abb4b153a26165e18`
- **window:** `2026-02-06T00:14:31Z` → `2026-02-06T00:45:55Z`
- **auth_mode / viewer_mode shown:** `unauth` (public AppView)

## Targets (from `05_manifest/run_metadata.csv`)

- `posts_per_feed = 20`
- `n_discovery = 5`
- `n_popular = 5`
- `n_less_known = 5`

## XRPC endpoints used (names only)

- **Relay enumeration**
  - `com.atproto.sync.listReposByCollection`
- **AppView reads**
  - `app.bsky.feed.getActorFeeds`
  - `app.bsky.feed.getFeedGenerators`
  - `app.bsky.graph.searchStarterPacks` *(probe returned 404 on this AppView; relay-based enumeration is used regardless)*
  - `app.bsky.graph.getActorStarterPacks`
  - `app.bsky.graph.getStarterPack`
  - `app.bsky.unspecced.getPopularFeedGenerators`
  - `app.bsky.feed.getFeed`
  - `app.bsky.actor.getProfiles`
- **Auth-only (not used in this run)**
  - `com.atproto.server.createSession`
  - `com.atproto.server.refreshSession`

## Produced artifacts (where + join keys)

- **Core exports:** `02_csv_exports/`
  - `feed_panel.csv` joins to `feed_items.csv.gz` on `feed_uri`
  - `feed_items.csv.gz` joins to `posts.csv.gz` on (`post_uri`, `post_cid`)
  - `posts.csv.gz` joins to `authors.csv.gz` on `author_did`
  - `post_labels.csv.gz` attaches to impressions via (`feed_uri`, `viewer_mode`, `post_uri`, `post_cid`)
- **State DB:** `state.db` (copy also in `01_state_db/state.db`)
- **Postprocess (stored, not presented):** `03_postprocess_metrics/` (joins + derived H-metric tables)
- **Logs:** `04_logs/`
- **Manifest + schema receipts:** `05_manifest/` (`manifest.csv`, `data_dictionary.csv`, `run_metadata.csv`, `run_summary.csv`, `validation_report.csv`)
- **Deck receipts (PNGs):** `06_figures_preview/receipts/`
- **Archive ZIP:** `07_archive_zip/`

## Read-only guarantee

Collector only performs read-only XRPC **GET** requests to Relay/AppView; it does **not** post, like, follow, label, or otherwise interact.
Only possible **POST** requests are session create/refresh when `auth_mode` is enabled (not used in this run).

