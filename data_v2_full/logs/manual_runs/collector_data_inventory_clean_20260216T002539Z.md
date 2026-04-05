# Collector Data Inventory (clean) 20260216T002539Z

Base: `data_v2_full`
CSV datasets: 20
JSONL datasets: 6
SQLite datasets: 4

## CSV
### `authors/{date}/author_profiles_part_{n}.csv`
- Example file: `authors/2026-02-16/author_profiles_part_000.csv`
- Columns (9): `run_id`, `vantage_id`, `author_did`, `handle`, `display_name`, `followers_count`, `follows_count`, `posts_count`, `captured_at_utc`
  - `run_id` = `128ee03368b04c509fad63e54f0884d3`
  - `vantage_id` = `unauth_enUS`
  - `author_did` = `did:plc:j7hvijonlcr4xw3upln3scyg`
  - `handle` = `bulebon.bsky.social`
  - `display_name` = `チャオ🇯🇵`
  - `followers_count` = `249`
  - `follows_count` = `203`
  - `posts_count` = `1357`
  - `captured_at_utc` = `2026-02-16T00:07:44Z`

### `hourly/{date}/{hour}/http_stats.csv`
- Example file: `hourly/2026-02-16/00/http_stats.csv`
- Columns (7): `timestamp_utc`, `endpoint`, `status_code`, `latency_ms`, `attempt`, `error_type`, `feed_uri`
  - `timestamp_utc` = `2026-02-16T00:05:11Z`
  - `endpoint` = `com.atproto.server.createSession`
  - `status_code` = `200`
  - `latency_ms` = `631.788`
  - `attempt` = `0`
  - `error_type` = ``
  - `feed_uri` = ``

### `hourly/{date}/{hour}/parts/feed_items_part_{n}.csv`
- Example file: `hourly/2026-02-16/00/parts/feed_items_part_013.csv`
- Columns (14): `run_id`, `snapshot_hour_utc`, `captured_at_utc`, `viewer_mode`, `vantage_id`, `feed_uri`, `bucket`, `rank`, `post_uri`, `post_cid`, `author_did`, `author_handle`, `reason_type`, `reason_actor_did`
  - `run_id` = `bb761f2aecb3439fade4b7ec7060b756`
  - `snapshot_hour_utc` = `2026-02-16T00:00:00Z`
  - `captured_at_utc` = `2026-02-16T00:18:49Z`
  - `viewer_mode` = `auth`
  - `vantage_id` = `auth_enUS`
  - `feed_uri` = `at://did:plc:2lhekjiheh2vrghdwmnoolya/app.bsky.feed.generator/aaai3qv3v5niq`
  - `bucket` = `popular_by_likecount`
  - `rank` = `1`
  - `post_uri` = `at://did:plc:seo2jm5izkrgmrv4n2dmp2sm/app.bsky.feed.post/3mew5zkigas2d`
  - `post_cid` = `bafyreidyohiiazbe3pv6lcqb34odseic2r6gfucmah6a5dpokhnxyat66q`
  - `author_did` = `did:plc:seo2jm5izkrgmrv4n2dmp2sm`
  - `author_handle` = `haru9000th.bsky.social`
  - `reason_type` = ``
  - `reason_actor_did` = ``

### `hourly/{date}/{hour}/parts/post_labels_part_{n}.csv`
- Example file: `hourly/2026-02-16/00/parts/post_labels_part_013.csv`
- Columns (12): `run_id`, `snapshot_hour_utc`, `captured_at_utc`, `viewer_mode`, `vantage_id`, `post_uri`, `post_cid`, `label_src`, `label_val`, `label_neg`, `label_uri`, `label_cts`
  - `run_id` = `bb761f2aecb3439fade4b7ec7060b756`
  - `snapshot_hour_utc` = `2026-02-16T00:00:00Z`
  - `captured_at_utc` = `2026-02-16T00:19:02Z`
  - `viewer_mode` = `auth`
  - `vantage_id` = `auth_enUS`
  - `post_uri` = `at://did:plc:2ngdnmnutiwistfzrf2db32k/app.bsky.feed.post/3lauvc7hcbs2h`
  - `post_cid` = `bafyreidhcwkkv73zeja3j3nm376433f5o3ttd2lpwpm5fun2dqn32mitly`
  - `label_src` = `did:plc:2ngdnmnutiwistfzrf2db32k`
  - `label_val` = `sexual`
  - `label_neg` = ``
  - `label_uri` = `at://did:plc:2ngdnmnutiwistfzrf2db32k/app.bsky.feed.post/3lauvc7hcbs2h`
  - `label_cts` = `2024-11-14T03:12:00.448Z`

### `hourly/{date}/{hour}/parts/post_metrics_part_{n}.csv`
- Example file: `hourly/2026-02-16/00/parts/post_metrics_part_013.csv`
- Columns (10): `run_id`, `snapshot_hour_utc`, `captured_at_utc`, `viewer_mode`, `vantage_id`, `post_uri`, `like_count`, `repost_count`, `reply_count`, `quote_count`
  - `run_id` = `bb761f2aecb3439fade4b7ec7060b756`
  - `snapshot_hour_utc` = `2026-02-16T00:00:00Z`
  - `captured_at_utc` = `2026-02-16T00:18:49Z`
  - `viewer_mode` = `auth`
  - `vantage_id` = `auth_enUS`
  - `post_uri` = `at://did:plc:seo2jm5izkrgmrv4n2dmp2sm/app.bsky.feed.post/3mew5zkigas2d`
  - `like_count` = `2`
  - `repost_count` = `0`
  - `reply_count` = `0`
  - `quote_count` = `0`

### `hourly/{date}/{hour}/parts/posts_first_seen_part_{n}.csv`
- Example file: `hourly/2026-02-16/00/parts/posts_first_seen_part_000.csv`
- Columns (14): `run_id`, `snapshot_hour_utc`, `captured_at_utc`, `viewer_mode`, `vantage_id`, `feed_uri`, `bucket`, `post_uri`, `post_cid`, `author_did`, `author_handle`, `record_created_at`, `indexed_at`, `text`
  - `run_id` = `bb761f2aecb3439fade4b7ec7060b756`
  - `snapshot_hour_utc` = `2026-02-16T00:00:00Z`
  - `captured_at_utc` = `2026-02-16T00:05:12Z`
  - `viewer_mode` = `unauth`
  - `vantage_id` = `unauth_enUS`
  - `feed_uri` = `at://did:plc:2232udxwy7iah3ow3zprhup4/app.bsky.feed.generator/aaalr5v4docfo`
  - `bucket` = `popular_by_likecount`
  - `post_uri` = `at://did:plc:2232udxwy7iah3ow3zprhup4/app.bsky.feed.post/3kncwowg4cs2g`
  - `post_cid` = `bafyreic5h772awti3kh2zlxo5qve74i4gf6qnmd5wpwobcftnaoty755ta`
  - `author_did` = `did:plc:2232udxwy7iah3ow3zprhup4`
  - `author_handle` = `reddline.bsky.social`
  - `record_created_at` = `2024-03-10T04:50:24.253Z`
  - `indexed_at` = `2024-03-10T04:50:24.253Z`
  - `text` = `Managed to fight through the ADHD today and get the handpainting done for this belt on the new model's alternate outfit 🎨🐦✨

#b3D #substance`

### `metadata/{date}/feed_catalog.csv`
- Example file: `metadata/2026-02-15/feed_catalog.csv`
- Columns (9): `feed_uri`, `creator_did`, `service_did`, `provider_domain`, `like_count_last`, `discovered_from_json`, `first_seen_utc`, `last_seen_utc`, `last_hydrated_utc`
  - `feed_uri` = `at://did:plc:3guzzweuqraryl3rdkimjamk/app.bsky.feed.generator/for-you`
  - `creator_did` = `did:plc:3guzzweuqraryl3rdkimjamk`
  - `service_did` = `did:web:linklonk.com`
  - `provider_domain` = `linklonk.com`
  - `like_count_last` = `43827`
  - `discovered_from_json` = `["popular_feed_generators", "suggested_feeds"]`
  - `first_seen_utc` = `2026-02-14T08:53:17Z`
  - `last_seen_utc` = `2026-02-15T16:22:43Z`
  - `last_hydrated_utc` = ``

### `metadata/{date}/feed_generators_index/http_stats.csv`
- Example file: `metadata/2026-02-16/feed_generators_index/http_stats.csv`
- Columns (7): `timestamp_utc`, `endpoint`, `status_code`, `latency_ms`, `attempt`, `error_type`, `feed_uri`
  - `timestamp_utc` = `2026-02-16T00:18:47Z`
  - `endpoint` = `com.atproto.server.createSession`
  - `status_code` = `200`
  - `latency_ms` = `350.81`
  - `attempt` = `0`
  - `error_type` = ``
  - `feed_uri` = ``

### `metadata/{date}/starterpack_accounts.csv`
- Example file: `metadata/2026-02-15/starterpack_accounts.csv`
- Columns (7): `pack_uri`, `list_uri`, `subject_did`, `position`, `captured_at_utc`, `vantage_id`, `source`
  - `pack_uri` = `at://did:plc:pifkcjimdcfwaxkanzhwxufp/app.bsky.graph.starterpack/3lylqhd3un52v`
  - `list_uri` = `at://did:plc:pifkcjimdcfwaxkanzhwxufp/app.bsky.graph.list/3lylqhcnxg32i`
  - `subject_did` = `did:plc:7z6ecty3w4vsovvvhlv7nnee`
  - `position` = `0`
  - `captured_at_utc` = `2026-02-15T16:22:43Z`
  - `vantage_id` = `auth`
  - `source` = `onboarding_suggested_starterpacks`

### `metadata/{date}/starterpack_feeds.csv`
- Example file: `metadata/2026-02-15/starterpack_feeds.csv`
- Columns (9): `pack_uri`, `pack_creator`, `joinedWeekCount`, `joinedAllTimeCount`, `feed_uri`, `slot_index`, `captured_at_utc`, `vantage_id`, `source`
  - `pack_uri` = `(no data row)`
  - `pack_creator` = `(no data row)`
  - `joinedWeekCount` = `(no data row)`
  - `joinedAllTimeCount` = `(no data row)`
  - `feed_uri` = `(no data row)`
  - `slot_index` = `(no data row)`
  - `captured_at_utc` = `(no data row)`
  - `vantage_id` = `(no data row)`
  - `source` = `(no data row)`

### `metadata/{date}/suggested_accounts.csv`
- Example file: `metadata/2026-02-15/suggested_accounts.csv`
- Columns (4): `actor_did`, `position`, `captured_at_utc`, `vantage_id`
  - `actor_did` = `(no data row)`
  - `position` = `(no data row)`
  - `captured_at_utc` = `(no data row)`
  - `vantage_id` = `(no data row)`

### `metadata/{date}/suggested_feeds.csv`
- Example file: `metadata/2026-02-15/suggested_feeds.csv`
- Columns (4): `feed_uri`, `position`, `captured_at_utc`, `vantage_id`
  - `feed_uri` = `at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot`
  - `position` = `0`
  - `captured_at_utc` = `2026-02-15T16:22:43Z`
  - `vantage_id` = `auth`

### `metadata/{date}/suggested_follows_by_actor.csv`
- Example file: `metadata/2026-02-15/suggested_follows_by_actor.csv`
- Columns (6): `seed_actor_did`, `suggested_did`, `position`, `isFallback`, `captured_at_utc`, `vantage_id`
  - `seed_actor_did` = `(no data row)`
  - `suggested_did` = `(no data row)`
  - `position` = `(no data row)`
  - `isFallback` = `(no data row)`
  - `captured_at_utc` = `(no data row)`
  - `vantage_id` = `(no data row)`

### `panel/panel_v1.csv`
- Example file: `panel/panel_v1.csv`
- Columns (5): `feed_uri`, `bucket`, `unauth_skip`, `built_at_utc`, `panel_version_id`
  - `feed_uri` = `at://did:plc:3guzzweuqraryl3rdkimjamk/app.bsky.feed.generator/for-you`
  - `bucket` = `popular_by_likecount`
  - `unauth_skip` = `0`
  - `built_at_utc` = `2026-02-15T16:27:07Z`
  - `panel_version_id` = `2026-02-15`

### `panel/panel_versions/panel_v1_{date}.csv`
- Example file: `panel/panel_versions/panel_v1_2026-02-15.csv`
- Columns (5): `feed_uri`, `bucket`, `unauth_skip`, `built_at_utc`, `panel_version_id`
  - `feed_uri` = `at://did:plc:3guzzweuqraryl3rdkimjamk/app.bsky.feed.generator/for-you`
  - `bucket` = `popular_by_likecount`
  - `unauth_skip` = `0`
  - `built_at_utc` = `2026-02-15T16:27:07Z`
  - `panel_version_id` = `2026-02-15`

### `wide/{date}/http_stats.csv`
- Example file: `wide/2026-02-16/http_stats.csv`
- Columns (7): `timestamp_utc`, `endpoint`, `status_code`, `latency_ms`, `attempt`, `error_type`, `feed_uri`
  - `timestamp_utc` = `2026-02-16T00:06:24Z`
  - `endpoint` = `app.bsky.feed.getFeed`
  - `status_code` = `200`
  - `latency_ms` = `505.565`
  - `attempt` = `0`
  - `error_type` = ``
  - `feed_uri` = `at://did:plc:2tdgzwjdfgomblcjywnis6ab/app.bsky.feed.generator/aaaiexvr2wzgg`

### `wide/{date}/parts/feed_items_part_{n}.csv`
- Example file: `wide/2026-02-16/parts/feed_items_part_011.csv`
- Columns (14): `run_id`, `snapshot_hour_utc`, `captured_at_utc`, `viewer_mode`, `vantage_id`, `feed_uri`, `bucket`, `rank`, `post_uri`, `post_cid`, `author_did`, `author_handle`, `reason_type`, `reason_actor_did`
  - `run_id` = `1ad74b51f5b54d0d86c1db93a8c530b7`
  - `snapshot_hour_utc` = `2026-02-16T00:00:00Z`
  - `captured_at_utc` = `2026-02-16T00:18:47Z`
  - `viewer_mode` = `unauth`
  - `vantage_id` = `unauth_enUS`
  - `feed_uri` = `at://did:plc:3l4bgvabtwvrbr6z4i6tfcbq/app.bsky.feed.generator/aaaghw3zg36uk`
  - `bucket` = `wide_sweep`
  - `rank` = `1`
  - `post_uri` = `at://did:plc:hbpnlz2fpiq5inemwuv2fmyw/app.bsky.feed.post/3mevu5spdtk2c`
  - `post_cid` = `bafyreicf7y6pt723bbxf5cgz557buje6bubi66dnm66tjwfpukcin6bdgi`
  - `author_did` = `did:plc:hbpnlz2fpiq5inemwuv2fmyw`
  - `author_handle` = `waki89.bsky.social`
  - `reason_type` = ``
  - `reason_actor_did` = ``

### `wide/{date}/parts/post_labels_part_{n}.csv`
- Example file: `wide/2026-02-16/parts/post_labels_part_004.csv`
- Columns (12): `run_id`, `snapshot_hour_utc`, `captured_at_utc`, `viewer_mode`, `vantage_id`, `post_uri`, `post_cid`, `label_src`, `label_val`, `label_neg`, `label_uri`, `label_cts`
  - `run_id` = `1ad74b51f5b54d0d86c1db93a8c530b7`
  - `snapshot_hour_utc` = `2026-02-16T00:00:00Z`
  - `captured_at_utc` = `2026-02-16T00:18:50Z`
  - `viewer_mode` = `unauth`
  - `vantage_id` = `unauth_enUS`
  - `post_uri` = `at://did:plc:qmxglj4cysio7kxnjqgeaajt/app.bsky.feed.post/3lytd2nsllc2c`
  - `post_cid` = `bafyreifg7cfce2jdmtqubkx3dngitidnl3kbiodlg3jmpzejnmp4c664sa`
  - `label_src` = `did:plc:ar7c4by46qjdydhdevvrndac`
  - `label_val` = `porn`
  - `label_neg` = ``
  - `label_uri` = `at://did:plc:qmxglj4cysio7kxnjqgeaajt/app.bsky.feed.post/3lytd2nsllc2c`
  - `label_cts` = `2025-09-14T22:17:43.446Z`

### `wide/{date}/parts/post_metrics_part_{n}.csv`
- Example file: `wide/2026-02-16/parts/post_metrics_part_011.csv`
- Columns (10): `run_id`, `snapshot_hour_utc`, `captured_at_utc`, `viewer_mode`, `vantage_id`, `post_uri`, `like_count`, `repost_count`, `reply_count`, `quote_count`
  - `run_id` = `1ad74b51f5b54d0d86c1db93a8c530b7`
  - `snapshot_hour_utc` = `2026-02-16T00:00:00Z`
  - `captured_at_utc` = `2026-02-16T00:18:47Z`
  - `viewer_mode` = `unauth`
  - `vantage_id` = `unauth_enUS`
  - `post_uri` = `at://did:plc:hbpnlz2fpiq5inemwuv2fmyw/app.bsky.feed.post/3mevu5spdtk2c`
  - `like_count` = `1`
  - `repost_count` = `0`
  - `reply_count` = `0`
  - `quote_count` = `0`

### `wide/{date}/parts/posts_first_seen_part_{n}.csv`
- Example file: `wide/2026-02-16/parts/posts_first_seen_part_011.csv`
- Columns (14): `run_id`, `snapshot_hour_utc`, `captured_at_utc`, `viewer_mode`, `vantage_id`, `feed_uri`, `bucket`, `post_uri`, `post_cid`, `author_did`, `author_handle`, `record_created_at`, `indexed_at`, `text`
  - `run_id` = `1ad74b51f5b54d0d86c1db93a8c530b7`
  - `snapshot_hour_utc` = `2026-02-16T00:00:00Z`
  - `captured_at_utc` = `2026-02-16T00:18:51Z`
  - `viewer_mode` = `unauth`
  - `vantage_id` = `unauth_enUS`
  - `feed_uri` = `at://did:plc:3sn6bogankaicebzdb7liro3/app.bsky.feed.generator/aaaeiwqgi5frc`
  - `bucket` = `wide_sweep`
  - `post_uri` = `at://did:plc:xtrzqicesfwterpbweugpl4u/app.bsky.feed.post/3mewaocij3223`
  - `post_cid` = `bafyreifnbkrjlmukemvhr2hvdepoo2rsq3xmlu6ypwyjsyecz3blsh6qpu`
  - `author_did` = `did:plc:xtrzqicesfwterpbweugpl4u`
  - `author_handle` = `altcdc.altgov.info`
  - `record_created_at` = `2026-02-15T19:15:15.797Z`
  - `indexed_at` = `2026-02-15T19:15:17.550Z`
  - `text` = `100% agree! 😢 We miss when our comms could... comm.`

## JSONL
### `metadata/{date}/discovery_sources/onboarding_suggested_starterpacks.jsonl`
- Example file: `metadata/2026-02-15/discovery_sources/onboarding_suggested_starterpacks.jsonl`
- Top-level keys (7): `captured_at_utc`, `source`, `method`, `host`, `position`, `item`, `vantage_id`
  - `captured_at_utc` = `2026-02-15T16:22:43Z`
  - `source` = `onboarding_suggested_starterpacks`
  - `method` = `app.bsky.unspecced.getOnboardingSuggestedStarterPacks`
  - `host` = `https://public.api.bsky.app`
  - `position` = `0`
  - `item` = `{'uri': 'at://did:plc:pifkcjimdcfwaxkanzhwxufp/app.bsky.graph.starterpack/3lylqhd3un52v', 'cid': 'bafyreifbn4wfayabyrbcbvcgbwfupwe6dtpmqvicdgdzw3vtcqpwssyspa...`
  - `vantage_id` = `auth`

### `metadata/{date}/discovery_sources/popular_feed_generators.jsonl`
- Example file: `metadata/2026-02-15/discovery_sources/popular_feed_generators.jsonl`
- Top-level keys (3): `captured_at_utc`, `source`, `item`
  - `captured_at_utc` = `2026-02-15T16:22:43Z`
  - `source` = `popular`
  - `item` = `{'uri': 'at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot', 'cid': 'bafyreievgu2ty7qbiaaom5zhmkznsnajuzideek3lo7e65dwqlrvrxnmo4', 'did...`

### `metadata/{date}/discovery_sources/suggested_accounts.jsonl`
- Example file: `metadata/2026-02-15/discovery_sources/suggested_accounts.jsonl`
- Top-level keys (0): 

### `metadata/{date}/discovery_sources/suggested_feeds.jsonl`
- Example file: `metadata/2026-02-15/discovery_sources/suggested_feeds.jsonl`
- Top-level keys (5): `captured_at_utc`, `source`, `position`, `item`, `vantage_id`
  - `captured_at_utc` = `2026-02-15T16:22:43Z`
  - `source` = `suggested_feeds`
  - `position` = `0`
  - `item` = `{'uri': 'at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot', 'cid': 'bafyreievgu2ty7qbiaaom5zhmkznsnajuzideek3lo7e65dwqlrvrxnmo4', 'did...`
  - `vantage_id` = `auth`

### `metadata/{date}/discovery_sources/suggested_follows_by_actor.jsonl`
- Example file: `metadata/2026-02-15/discovery_sources/suggested_follows_by_actor.jsonl`
- Top-level keys (0): 

### `metadata/{date}/feed_generators_index/parts/feed_generators_part_{n}.jsonl`
- Example file: `metadata/2026-02-16/feed_generators_index/parts/feed_generators_part_000000.jsonl`
- Top-level keys (6): `captured_at_utc`, `part_index`, `repo_did`, `records_cursor_start`, `position`, `record`
  - `captured_at_utc` = `2026-02-16T00:19:02Z`
  - `part_index` = `0`
  - `repo_did` = `did:plc:fqtgb2laaxgszz42s6dqgg2r`
  - `records_cursor_start` = `NULL`
  - `position` = `0`
  - `record` = `{'uri': 'at://did:plc:fqtgb2laaxgszz42s6dqgg2r/app.bsky.feed.generator/aaanrnqgsgtvu', 'cid': 'bafyreigkb5p43deplvv4ouadkp2i7hikb72mjkg2hd2pzmy4pcdmq4kbfy', ...`

## SQLITE
### `control/control_state.backup.db`
- Example file: `control/control_state.backup.db`
- Tables (9):
  - `author_registry` columns (5): `author_did`, `first_seen_utc`, `last_seen_utc`, `seen_count`, `last_hydrated_utc`
    - `author_did` = `did:plc:2gx2noukxzwmj6dkxbmh3qt5`
    - `first_seen_utc` = `2026-02-14T09:19:35Z`
    - `last_seen_utc` = `2026-02-14T23:12:45Z`
    - `seen_count` = `8`
    - `last_hydrated_utc` = `2026-02-14T09:20:29Z`
  - `feed_catalog` columns (9): `feed_uri`, `creator_did`, `service_did`, `provider_domain`, `like_count_last`, `discovered_from`, `first_seen_utc`, `last_seen_utc`, `last_hydrated_utc`
    - `feed_uri` = `at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot`
    - `creator_did` = `did:plc:z72i7hdynmk6r22z27h6tvur`
    - `service_did` = `did:web:discover.bsky.app`
    - `provider_domain` = `discover.bsky.app`
    - `like_count_last` = `38668`
    - `discovered_from` = `["popular_feed_generators", "suggested_feeds"]`
    - `first_seen_utc` = `2026-02-14T08:53:17Z`
    - `last_seen_utc` = `2026-02-14T22:30:17Z`
    - `last_hydrated_utc` = `NULL`
  - `feed_generator_index_global` columns (5): `collection`, `repo_source`, `repos_cursor`, `repos_done`, `updated_at_utc`
    - `collection` = `app.bsky.feed.generator`
    - `repo_source` = `https://bsky.network::listRepos`
    - `repos_cursor` = `42927`
    - `repos_done` = `0`
    - `updated_at_utc` = `2026-02-15T01:30:07Z`
  - `feed_generator_index_parts` columns (8): `collection`, `date_utc`, `part_index`, `status`, `started_at_utc`, `finished_at_utc`, `n_records`, `last_error`
    - `collection` = `app.bsky.feed.generator`
    - `date_utc` = `2026-02-14`
    - `part_index` = `0`
    - `status` = `success`
    - `started_at_utc` = `2026-02-14T22:47:04Z`
    - `finished_at_utc` = `2026-02-14T22:47:04Z`
    - `n_records` = `1`
    - `last_error` = `NULL`
  - `feed_generator_index_repo_tasks` columns (8): `collection`, `repo_did`, `status`, `cursor`, `attempts`, `last_error`, `first_seen_utc`, `updated_at_utc`
    - `collection` = `app.bsky.feed.generator`
    - `repo_did` = `did:plc:nfd3tswpp7vne6btqcv5p6wr`
    - `status` = `failed`
    - `cursor` = `NULL`
    - `attempts` = `3`
    - `last_error` = `HttpError('InvalidRequest: Could not find repo: did:plc:nfd3tswpp7vne6btqcv5p6wr')`
    - `first_seen_utc` = `2026-02-14T22:46:53Z`
    - `updated_at_utc` = `2026-02-14T22:48:05Z`
  - `post_registry` columns (5): `post_uri`, `first_seen_utc`, `last_seen_utc`, `seen_count`, `first_written`
    - `post_uri` = `at://did:plc:2gx2noukxzwmj6dkxbmh3qt5/app.bsky.feed.post/3k6y544xn6w2f`
    - `first_seen_utc` = `2026-02-14T09:19:35Z`
    - `last_seen_utc` = `2026-02-14T23:12:45Z`
    - `seen_count` = `4`
    - `first_written` = `1`
  - `queue_posts` columns (9): `post_uri`, `first_seen_utc`, `priority`, `status_likes`, `status_reposts`, `status_quotes`, `status_replies`, `last_error`, `updated_at`
    - `post_uri` = `(no row)`
    - `first_seen_utc` = `(no row)`
    - `priority` = `(no row)`
    - `status_likes` = `(no row)`
    - `status_reposts` = `(no row)`
    - `status_quotes` = `(no row)`
    - `status_replies` = `(no row)`
    - `last_error` = `(no row)`
    - `updated_at` = `(no row)`
  - `runs` columns (6): `run_id`, `job_name`, `started_at_utc`, `finished_at_utc`, `params_json`, `success`
    - `run_id` = `b66e6cb313e54c4295aa46ff87179d9c`
    - `job_name` = `refresh-discovery`
    - `started_at_utc` = `2026-02-14T08:53:17Z`
    - `finished_at_utc` = `2026-02-14T08:53:21Z`
    - `params_json` = `{}`
    - `success` = `1`
  - `wide_sweep_tasks` columns (8): `date_utc`, `feed_uri`, `status`, `attempts`, `last_error`, `updated_at_utc`, `started_at_utc`, `finished_at_utc`
    - `date_utc` = `2026-02-14`
    - `feed_uri` = `at://did:plc:2qlbivzvdt6hp6dwrd2sx4sf/app.bsky.feed.generator/aaaemkt7ysfq4`
    - `status` = `success`
    - `attempts` = `1`
    - `last_error` = `NULL`
    - `updated_at_utc` = `2026-02-14T09:20:06Z`
    - `started_at_utc` = `2026-02-14T09:20:06Z`
    - `finished_at_utc` = `2026-02-14T09:20:06Z`

### `control/control_state.db`
- Example file: `control/control_state.db`
- Tables (9):
  - `author_registry` columns (5): `author_did`, `first_seen_utc`, `last_seen_utc`, `seen_count`, `last_hydrated_utc`
    - `author_did` = `did:plc:2gx2noukxzwmj6dkxbmh3qt5`
    - `first_seen_utc` = `2026-02-14T09:19:35Z`
    - `last_seen_utc` = `2026-02-16T00:16:57Z`
    - `seen_count` = `88`
    - `last_hydrated_utc` = `2026-02-14T09:20:29Z`
  - `feed_catalog` columns (9): `feed_uri`, `creator_did`, `service_did`, `provider_domain`, `like_count_last`, `discovered_from`, `first_seen_utc`, `last_seen_utc`, `last_hydrated_utc`
    - `feed_uri` = `at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot`
    - `creator_did` = `did:plc:z72i7hdynmk6r22z27h6tvur`
    - `service_did` = `did:web:discover.bsky.app`
    - `provider_domain` = `discover.bsky.app`
    - `like_count_last` = `38672`
    - `discovered_from` = `["popular_feed_generators", "suggested_feeds"]`
    - `first_seen_utc` = `2026-02-14T08:53:17Z`
    - `last_seen_utc` = `2026-02-15T16:22:43Z`
    - `last_hydrated_utc` = `NULL`
  - `feed_generator_index_global` columns (5): `collection`, `repo_source`, `repos_cursor`, `repos_done`, `updated_at_utc`
    - `collection` = `app.bsky.feed.generator`
    - `repo_source` = `https://bsky.network::listRepos`
    - `repos_cursor` = `268571`
    - `repos_done` = `0`
    - `updated_at_utc` = `2026-02-15T16:30:50Z`
  - `feed_generator_index_parts` columns (8): `collection`, `date_utc`, `part_index`, `status`, `started_at_utc`, `finished_at_utc`, `n_records`, `last_error`
    - `collection` = `app.bsky.feed.generator`
    - `date_utc` = `2026-02-14`
    - `part_index` = `0`
    - `status` = `success`
    - `started_at_utc` = `2026-02-14T22:47:04Z`
    - `finished_at_utc` = `2026-02-14T22:47:04Z`
    - `n_records` = `1`
    - `last_error` = `NULL`
  - `feed_generator_index_repo_tasks` columns (8): `collection`, `repo_did`, `status`, `cursor`, `attempts`, `last_error`, `first_seen_utc`, `updated_at_utc`
    - `collection` = `app.bsky.feed.generator`
    - `repo_did` = `did:plc:nfd3tswpp7vne6btqcv5p6wr`
    - `status` = `failed`
    - `cursor` = `NULL`
    - `attempts` = `3`
    - `last_error` = `HttpError('InvalidRequest: Could not find repo: did:plc:nfd3tswpp7vne6btqcv5p6wr')`
    - `first_seen_utc` = `2026-02-14T22:46:53Z`
    - `updated_at_utc` = `2026-02-14T22:48:05Z`
  - `post_registry` columns (5): `post_uri`, `first_seen_utc`, `last_seen_utc`, `seen_count`, `first_written`
    - `post_uri` = `at://did:plc:2gx2noukxzwmj6dkxbmh3qt5/app.bsky.feed.post/3k6y544xn6w2f`
    - `first_seen_utc` = `2026-02-14T09:19:35Z`
    - `last_seen_utc` = `2026-02-16T00:15:43Z`
    - `seen_count` = `8`
    - `first_written` = `1`
  - `queue_posts` columns (9): `post_uri`, `first_seen_utc`, `priority`, `status_likes`, `status_reposts`, `status_quotes`, `status_replies`, `last_error`, `updated_at`
    - `post_uri` = `(no row)`
    - `first_seen_utc` = `(no row)`
    - `priority` = `(no row)`
    - `status_likes` = `(no row)`
    - `status_reposts` = `(no row)`
    - `status_quotes` = `(no row)`
    - `status_replies` = `(no row)`
    - `last_error` = `(no row)`
    - `updated_at` = `(no row)`
  - `runs` columns (6): `run_id`, `job_name`, `started_at_utc`, `finished_at_utc`, `params_json`, `success`
    - `run_id` = `b66e6cb313e54c4295aa46ff87179d9c`
    - `job_name` = `refresh-discovery`
    - `started_at_utc` = `2026-02-14T08:53:17Z`
    - `finished_at_utc` = `2026-02-14T08:53:21Z`
    - `params_json` = `{}`
    - `success` = `1`
  - `wide_sweep_tasks` columns (8): `date_utc`, `feed_uri`, `status`, `attempts`, `last_error`, `updated_at_utc`, `started_at_utc`, `finished_at_utc`
    - `date_utc` = `2026-02-14`
    - `feed_uri` = `at://did:plc:2qlbivzvdt6hp6dwrd2sx4sf/app.bsky.feed.generator/aaaemkt7ysfq4`
    - `status` = `success`
    - `attempts` = `1`
    - `last_error` = `NULL`
    - `updated_at_utc` = `2026-02-14T09:20:06Z`
    - `started_at_utc` = `2026-02-14T09:20:06Z`
    - `finished_at_utc` = `2026-02-14T09:20:06Z`

### `hourly/{date}/{hour}/snapshot_status.backup.sqlite`
- Example file: `hourly/2026-02-14/23/snapshot_status.backup.sqlite`
- Tables (1):
  - `feed_tasks` columns (8): `feed_uri`, `viewer_mode`, `status`, `attempts`, `last_error`, `updated_at_utc`, `started_at_utc`, `finished_at_utc`
    - `feed_uri` = `at://did:plc:3guzzweuqraryl3rdkimjamk/app.bsky.feed.generator/for-you`
    - `viewer_mode` = `unauth`
    - `status` = `failed`
    - `attempts` = `1`
    - `last_error` = `HttpError('InvalidRequest: InvalidRequest')`
    - `updated_at_utc` = `2026-02-14T23:12:46Z`
    - `started_at_utc` = `2026-02-14T23:12:45Z`
    - `finished_at_utc` = `2026-02-14T23:12:46Z`

### `hourly/{date}/{hour}/snapshot_status.sqlite`
- Example file: `hourly/2026-02-16/00/snapshot_status.sqlite`
- Tables (1):
  - `feed_tasks` columns (8): `feed_uri`, `viewer_mode`, `status`, `attempts`, `last_error`, `updated_at_utc`, `started_at_utc`, `finished_at_utc`
    - `feed_uri` = `at://did:plc:3guzzweuqraryl3rdkimjamk/app.bsky.feed.generator/for-you`
    - `viewer_mode` = `unauth`
    - `status` = `pending`
    - `attempts` = `0`
    - `last_error` = `NULL`
    - `updated_at_utc` = `2026-02-16T00:05:11Z`
    - `started_at_utc` = `NULL`
    - `finished_at_utc` = `NULL`
