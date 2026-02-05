from __future__ import annotations

import csv
import gzip
import logging
from pathlib import Path
from typing import Iterable

from bsky_fair_collect.config import AppConfig
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import sha256_file, utc_now_iso

logger = logging.getLogger("bsky_fair_collect.write_outputs")


def _write_csv(path: Path, *, fieldnames: list[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _write_csv_gz(path: Path, *, fieldnames: list[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_run_metadata(cfg: AppConfig, state: StateDB, *, finished_at_utc: str) -> None:
    run_id = state.get_meta("run_id") or ""
    started_at = state.get_meta("started_at_utc") or ""
    rows = [
        {
            "run_id": run_id,
            "started_at_utc": started_at,
            "finished_at_utc": finished_at_utc,
            "appview_host": cfg.hosts.appview_host,
            "relay_host": cfg.hosts.relay_host,
            "auth_mode": cfg.auth_mode.value,
            "rps": cfg.run.rps,
            "max_retries": cfg.run.max_retries,
            "posts_per_feed": cfg.run.posts_per_feed,
            "n_discovery": cfg.run.n_discovery,
            "n_popular": cfg.run.n_popular,
            "n_less_known": cfg.run.n_less_known,
        }
    ]
    _write_csv(
        cfg.outputs.csv_dir / "run_metadata.csv",
        fieldnames=list(rows[0].keys()),
        rows=rows,
    )


def finalize_run_metadata(cfg: AppConfig, state: StateDB) -> None:
    state.set_meta("finished_at_utc", utc_now_iso())
    write_run_metadata(cfg, state, finished_at_utc=state.get_meta("finished_at_utc") or "")


def export_all_csvs(cfg: AppConfig, state: StateDB) -> None:
    logger.info("export_all_csvs: start")
    csv_dir = cfg.outputs.csv_dir

    _write_csv(
        csv_dir / "errors.csv",
        fieldnames=[
            "stage",
            "key",
            "error_type",
            "http_status",
            "error_message",
            "when_utc",
            "retry_count",
        ],
        rows=_iter_errors_rows(state),
    )

    _write_csv(
        csv_dir / "feed_generators_index.csv",
        fieldnames=[
            "feed_uri",
            "creator_did",
            "rkey",
            "service_did",
            "provider_bucket",
            "display_name",
            "description",
            "accepts_interaction",
            "content_mode",
            "indexed_at",
        ],
        rows=_iter_feed_generators_rows(state),
    )

    _write_csv(
        csv_dir / "starterpacks.csv",
        fieldnames=["starterpack_uri", "creator_did", "name", "description", "collected_at_utc"],
        rows=_iter_starterpacks_rows(state),
    )

    _write_csv(
        csv_dir / "starterpack_feeds.csv",
        fieldnames=["starterpack_uri", "slot_index", "feed_uri"],
        rows=_iter_starterpack_feeds_rows(state),
    )

    _write_csv(
        csv_dir / "discovery_feed_inclusions.csv",
        fieldnames=["feed_uri", "inclusion_count", "slot_count", "inclusion_rank"],
        rows=_iter_discovery_inclusions_rows(state),
    )

    _write_csv(
        csv_dir / "popular_feeds.csv",
        fieldnames=["feed_uri", "popularity_rank", "collected_at_utc"],
        rows=_iter_popular_feeds_rows(state),
    )

    _write_csv(
        csv_dir / "feed_panel.csv",
        fieldnames=[
            "feed_uri",
            "feed_group",
            "selection_reason",
            "provider_bucket",
            "service_did",
            "creator_did",
            "display_name",
            "inclusion_count",
            "popularity_rank",
        ],
        rows=_iter_feed_panel_rows(state),
    )

    _write_csv(
        csv_dir / "feed_snapshot_status.csv",
        fieldnames=[
            "feed_uri",
            "feed_group",
            "viewer_mode",
            "collected_at_utc",
            "requested_items",
            "returned_items",
            "pages_fetched",
            "success",
            "http_status",
            "error_type",
            "error_message_short",
        ],
        rows=_iter_feed_snapshot_status_rows(state),
    )

    _write_csv_gz(
        csv_dir / "feed_items.csv.gz",
        fieldnames=[
            "feed_uri",
            "feed_group",
            "viewer_mode",
            "collected_at_utc",
            "rank",
            "post_uri",
            "post_cid",
            "author_did",
            "author_handle",
            "reason_type",
            "reason_actor_did",
        ],
        rows=_iter_feed_items_rows(state),
    )

    _write_csv_gz(
        csv_dir / "posts.csv.gz",
        fieldnames=[
            "post_uri",
            "post_cid",
            "author_did",
            "author_handle",
            "record_created_at",
            "indexed_at",
            "text",
            "text_len",
            "is_reply",
            "reply_parent_uri",
            "reply_root_uri",
            "is_quote",
            "quoted_uri",
            "embed_type",
            "image_count",
            "external_uri",
            "external_domain",
            "facet_link_count",
            "link_domains_json",
            "mention_count",
            "hashtag_count",
            "like_count",
            "repost_count",
            "reply_count",
            "quote_count",
            "langs_json",
            "post_labels_json",
            "author_labels_json",
        ],
        rows=_iter_posts_rows(state),
    )

    _write_csv_gz(
        csv_dir / "post_labels.csv.gz",
        fieldnames=[
            "post_uri",
            "post_cid",
            "feed_uri",
            "viewer_mode",
            "collected_at_utc",
            "label_src",
            "label_val",
            "label_neg",
            "label_uri",
        ],
        rows=_iter_post_labels_rows(state),
    )

    _write_csv_gz(
        csv_dir / "authors.csv.gz",
        fieldnames=[
            "author_did",
            "handle",
            "display_name",
            "followers_count",
            "follows_count",
            "posts_count",
            "collected_at_utc",
        ],
        rows=_iter_authors_rows(state),
    )

    _write_csv(
        csv_dir / "provider_stats.csv",
        fieldnames=[
            "provider_bucket",
            "hosted_feed_count_api",
            "discovery_slot_count",
            "hosting_share",
            "discovery_share",
            "leverage_ratio",
        ],
        rows=_iter_provider_stats_rows(state),
    )

    _write_csv(
        csv_dir / "validation_report.csv",
        fieldnames=["check_name", "status", "observed_value", "expected_threshold", "notes"],
        rows=_iter_validation_rows(state),
    )

    _write_csv(
        csv_dir / "run_summary.csv",
        fieldnames=[
            "num_starterpacks_seen",
            "num_unique_feeds_from_starterpacks",
            "num_popular_feeds_seen",
            "num_feed_generators_indexed",
            "num_feeds_panel",
            "num_feeds_snapshotted_success",
            "num_feed_items",
            "num_unique_posts",
            "num_unique_authors",
            "auth_profile_hydration_rate",
            "snapshot_success_rate",
            "mapping_notes",
        ],
        rows=[_build_run_summary(cfg, state)],
    )

    _write_csv(
        csv_dir / "http_stats.csv",
        fieldnames=[
            "endpoint_name",
            "request_count",
            "success_count",
            "rate_limited_count",
            "avg_latency_ms",
            "p95_latency_ms",
        ],
        rows=_iter_http_stats_rows(state),
    )

    _write_csv(
        csv_dir / "data_dictionary.csv",
        fieldnames=["file_name", "column_name", "dtype_hint", "description"],
        rows=_iter_data_dictionary_rows(),
    )

    _write_csv(
        csv_dir / "manifest.csv",
        fieldnames=["file_name", "bytes", "sha256", "created_at_utc"],
        rows=_iter_manifest_rows(csv_dir),
    )

    logger.info("export_all_csvs: done")


def _iter_manifest_rows(csv_dir: Path) -> Iterable[dict[str, object]]:
    created_at = utc_now_iso()
    for path in sorted(csv_dir.glob("*.csv*")):
        if path.name == "manifest.csv":
            continue
        yield {
            "file_name": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "created_at_utc": created_at,
        }


def _iter_errors_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        "SELECT stage, key, error_type, http_status, error_message, when_utc, retry_count FROM errors ORDER BY id"
    ):
        yield dict(row)


def _iter_feed_generators_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT
          feed_uri,
          creator_did,
          rkey,
          service_did,
          provider_bucket,
          display_name,
          description,
          accepts_interaction,
          content_mode,
          indexed_at
        FROM feed_generators
        ORDER BY feed_uri
        """
    ):
        yield dict(row)


def _iter_starterpacks_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT starterpack_uri, creator_did, name, description, collected_at_utc
        FROM starterpacks
        ORDER BY starterpack_uri
        """
    ):
        yield dict(row)


def _iter_starterpack_feeds_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT starterpack_uri, slot_index, feed_uri
        FROM starterpack_feeds
        ORDER BY starterpack_uri, slot_index
        """
    ):
        yield dict(row)


def _iter_discovery_inclusions_rows(state: StateDB) -> Iterable[dict[str, object]]:
    rows = list(
        state.conn.execute(
            """
            SELECT feed_uri, COUNT(DISTINCT starterpack_uri) AS inclusion_count, COUNT(*) AS slot_count
            FROM starterpack_feeds
            GROUP BY feed_uri
            """
        )
    )
    items = [
        {
            "feed_uri": r["feed_uri"],
            "inclusion_count": int(r["inclusion_count"]),
            "slot_count": int(r["slot_count"]),
        }
        for r in rows
    ]
    items.sort(key=lambda d: (-int(d["slot_count"]), str(d["feed_uri"])))
    for idx, item in enumerate(items, start=1):
        yield {
            "feed_uri": item["feed_uri"],
            "inclusion_count": item["inclusion_count"],
            "slot_count": item["slot_count"],
            "inclusion_rank": idx,
        }


def _iter_popular_feeds_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT feed_uri, popularity_rank, collected_at_utc
        FROM popular_feeds
        ORDER BY popularity_rank
        """
    ):
        yield dict(row)


def _iter_feed_panel_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT
          feed_uri,
          feed_group,
          selection_reason,
          provider_bucket,
          service_did,
          creator_did,
          display_name,
          inclusion_count,
          popularity_rank
        FROM feed_panel
        ORDER BY feed_group, feed_uri
        """
    ):
        yield dict(row)


def _iter_feed_snapshot_status_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT
          feed_uri,
          feed_group,
          viewer_mode,
          collected_at_utc,
          requested_items,
          returned_items,
          pages_fetched,
          success,
          http_status,
          error_type,
          error_message_short
        FROM feed_snapshot_status
        ORDER BY feed_group, feed_uri, viewer_mode
        """
    ):
        yield dict(row)


def _iter_feed_items_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT
          feed_uri,
          feed_group,
          viewer_mode,
          collected_at_utc,
          rank,
          post_uri,
          post_cid,
          author_did,
          author_handle,
          reason_type,
          reason_actor_did
        FROM feed_items
        ORDER BY feed_uri, viewer_mode, rank
        """
    ):
        yield dict(row)


def _iter_posts_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT
          post_uri,
          post_cid,
          author_did,
          author_handle,
          record_created_at,
          indexed_at,
          text,
          text_len,
          is_reply,
          reply_parent_uri,
          reply_root_uri,
          is_quote,
          quoted_uri,
          embed_type,
          image_count,
          external_uri,
          external_domain,
          facet_link_count,
          link_domains_json,
          mention_count,
          hashtag_count,
          like_count,
          repost_count,
          reply_count,
          quote_count,
          langs_json,
          post_labels_json,
          author_labels_json
        FROM posts
        ORDER BY post_uri, post_cid
        """
    ):
        yield dict(row)


def _iter_post_labels_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT
          post_uri,
          post_cid,
          feed_uri,
          viewer_mode,
          collected_at_utc,
          label_src,
          label_val,
          label_neg,
          label_uri
        FROM post_labels
        ORDER BY post_uri, post_cid, feed_uri, viewer_mode, label_src, label_val
        """
    ):
        yield dict(row)


def _iter_authors_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT
          author_did,
          handle,
          display_name,
          followers_count,
          follows_count,
          posts_count,
          collected_at_utc
        FROM authors
        ORDER BY author_did
        """
    ):
        yield dict(row)


def _iter_provider_stats_rows(state: StateDB) -> Iterable[dict[str, object]]:
    total_feeds = int(state.conn.execute("SELECT COUNT(*) AS n FROM feed_generators").fetchone()["n"])
    total_slots = int(state.conn.execute("SELECT COUNT(*) AS n FROM starterpack_feeds").fetchone()["n"])

    hosted = {
        str(r["provider_bucket"]): int(r["n"])
        for r in state.conn.execute(
            "SELECT provider_bucket, COUNT(*) AS n FROM feed_generators GROUP BY provider_bucket"
        )
    }

    slots: dict[str, int] = {}
    for r in state.conn.execute(
        """
        SELECT COALESCE(f.provider_bucket, 'unknown') AS provider_bucket, COUNT(*) AS n
        FROM starterpack_feeds s
        LEFT JOIN feed_generators f ON f.feed_uri = s.feed_uri
        GROUP BY COALESCE(f.provider_bucket, 'unknown')
        """
    ):
        slots[str(r["provider_bucket"])] = int(r["n"])

    provider_buckets = sorted(set(hosted) | set(slots))
    for bucket in provider_buckets:
        hosted_count = hosted.get(bucket, 0)
        slot_count = slots.get(bucket, 0)
        hosting_share = (hosted_count / total_feeds) if total_feeds else 0.0
        discovery_share = (slot_count / total_slots) if total_slots else 0.0
        leverage_ratio = (discovery_share / hosting_share) if hosting_share else ""
        yield {
            "provider_bucket": bucket,
            "hosted_feed_count_api": hosted_count,
            "discovery_slot_count": slot_count,
            "hosting_share": hosting_share,
            "discovery_share": discovery_share,
            "leverage_ratio": leverage_ratio,
        }


def _iter_validation_rows(state: StateDB) -> Iterable[dict[str, object]]:
    for row in state.conn.execute(
        """
        SELECT check_name, status, observed_value, expected_threshold, notes
        FROM validations
        ORDER BY check_name
        """
    ):
        yield {
            "check_name": row["check_name"],
            "status": row["status"],
            "observed_value": row["observed_value"],
            "expected_threshold": row["expected_threshold"],
            "notes": row["notes"],
        }


def _build_run_summary(cfg: AppConfig, state: StateDB) -> dict[str, object]:
    c = state.conn
    num_starterpacks = int(c.execute("SELECT COUNT(*) AS n FROM starterpacks").fetchone()["n"])
    num_unique_feeds_from_starterpacks = int(
        c.execute("SELECT COUNT(DISTINCT feed_uri) AS n FROM starterpack_feeds").fetchone()["n"]
    )
    num_popular = int(c.execute("SELECT COUNT(*) AS n FROM popular_feeds").fetchone()["n"])
    num_generators = int(c.execute("SELECT COUNT(*) AS n FROM feed_generators").fetchone()["n"])
    num_panel = int(c.execute("SELECT COUNT(*) AS n FROM feed_panel").fetchone()["n"])

    num_snapshots_success = int(
        c.execute(
            """
            SELECT COUNT(*) AS n
            FROM feed_snapshot_status s
            INNER JOIN feed_panel p ON p.feed_uri = s.feed_uri
            WHERE s.success = 1
            """
        ).fetchone()["n"]
    )
    num_feed_items = int(c.execute("SELECT COUNT(*) AS n FROM feed_items").fetchone()["n"])
    num_posts = int(c.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"])
    num_authors = int(c.execute("SELECT COUNT(DISTINCT author_did) AS n FROM feed_items").fetchone()["n"])
    num_authors_hydrated = int(c.execute("SELECT COUNT(*) AS n FROM authors").fetchone()["n"])

    expected_snapshots = num_panel * (2 if cfg.auth_mode.value == "both" else 1)
    snapshot_success_rate = (num_snapshots_success / expected_snapshots) if expected_snapshots else 0.0
    auth_profile_hydration_rate = (num_authors_hydrated / num_authors) if num_authors else 0.0

    return {
        "num_starterpacks_seen": num_starterpacks,
        "num_unique_feeds_from_starterpacks": num_unique_feeds_from_starterpacks,
        "num_popular_feeds_seen": num_popular,
        "num_feed_generators_indexed": num_generators,
        "num_feeds_panel": num_panel,
        "num_feeds_snapshotted_success": num_snapshots_success,
        "num_feed_items": num_feed_items,
        "num_unique_posts": num_posts,
        "num_unique_authors": num_authors,
        "auth_profile_hydration_rate": auth_profile_hydration_rate,
        "snapshot_success_rate": snapshot_success_rate,
        "mapping_notes": (
            "Index: relay listReposByCollection(app.bsky.feed.generator) + appview getActorFeeds; "
            "Discovery: relay listReposByCollection(app.bsky.graph.starterpack) + appview getActorStarterPacks + getStarterPack; "
            "Popular: appview getPopularFeedGenerators; "
            "Snapshots: appview getFeed; Authors: appview getProfiles."
        ),
    }


def _iter_http_stats_rows(state: StateDB) -> Iterable[dict[str, object]]:
    c = state.conn
    endpoint_rows = list(
        c.execute(
            """
            SELECT endpoint_name, request_count, success_count, rate_limited_count, total_latency_ms
            FROM http_stats_endpoint
            ORDER BY endpoint_name
            """
        )
    )
    for r in endpoint_rows:
        endpoint = str(r["endpoint_name"])
        req = int(r["request_count"])
        total_latency_ms = float(r["total_latency_ms"])
        avg_ms = (total_latency_ms / req) if req else 0.0
        p95 = _p95_latency_ms(state, endpoint_name=endpoint, request_count=req)
        yield {
            "endpoint_name": endpoint,
            "request_count": req,
            "success_count": int(r["success_count"]),
            "rate_limited_count": int(r["rate_limited_count"]),
            "avg_latency_ms": avg_ms,
            "p95_latency_ms": p95,
        }


def _p95_latency_ms(state: StateDB, *, endpoint_name: str, request_count: int) -> float:
    if request_count <= 0:
        return 0.0
    target = int((0.95 * request_count) + 0.5)
    cumulative = 0
    rows = state.conn.execute(
        """
        SELECT bin_upper_ms, count
        FROM http_latency_hist
        WHERE endpoint_name = ?
        ORDER BY bin_upper_ms
        """,
        (endpoint_name,),
    ).fetchall()
    for r in rows:
        cumulative += int(r["count"])
        if cumulative >= target:
            return float(r["bin_upper_ms"])
    if rows:
        return float(rows[-1]["bin_upper_ms"])
    return 0.0


def _iter_data_dictionary_rows() -> Iterable[dict[str, object]]:
    for file_name, cols in _DATA_DICTIONARY.items():
        for col in cols:
            yield {
                "file_name": file_name,
                "column_name": col["column_name"],
                "dtype_hint": col["dtype_hint"],
                "description": col["description"],
            }


_DATA_DICTIONARY: dict[str, list[dict[str, str]]] = {
    "run_metadata.csv": [
        {"column_name": "run_id", "dtype_hint": "str", "description": "Stable run identifier for this OUT_DIR."},
        {"column_name": "started_at_utc", "dtype_hint": "str", "description": "Run start time (UTC, ISO-8601)."},
        {"column_name": "finished_at_utc", "dtype_hint": "str", "description": "Run finish time (UTC, ISO-8601)."},
        {"column_name": "appview_host", "dtype_hint": "str", "description": "AppView base host used for reads."},
        {"column_name": "relay_host", "dtype_hint": "str", "description": "Relay base host used for indexing."},
        {"column_name": "auth_mode", "dtype_hint": "str", "description": "unauth|auth|both."},
        {"column_name": "rps", "dtype_hint": "float", "description": "Client-side request rate limit (requests/sec)."},
        {"column_name": "max_retries", "dtype_hint": "int", "description": "Max retries for 429/5xx/network errors."},
        {"column_name": "posts_per_feed", "dtype_hint": "int", "description": "Requested ranked items per feed snapshot."},
        {"column_name": "n_discovery", "dtype_hint": "int", "description": "Target number of discovery_surfaced feeds."},
        {"column_name": "n_popular", "dtype_hint": "int", "description": "Target number of popular feeds."},
        {"column_name": "n_less_known", "dtype_hint": "int", "description": "Target number of less_known feeds."},
    ],
    "run_summary.csv": [
        {"column_name": "num_starterpacks_seen", "dtype_hint": "int", "description": "Starter packs collected."},
        {
            "column_name": "num_unique_feeds_from_starterpacks",
            "dtype_hint": "int",
            "description": "Distinct feed URIs observed in starter packs.",
        },
        {"column_name": "num_popular_feeds_seen", "dtype_hint": "int", "description": "Distinct feeds from popular feed endpoint."},
        {"column_name": "num_feed_generators_indexed", "dtype_hint": "int", "description": "Feed generators discovered via indexing."},
        {"column_name": "num_feeds_panel", "dtype_hint": "int", "description": "Feeds selected for snapshotting."},
        {"column_name": "num_feeds_snapshotted_success", "dtype_hint": "int", "description": "Successful snapshot rows."},
        {"column_name": "num_feed_items", "dtype_hint": "int", "description": "Total ranked feed items collected."},
        {"column_name": "num_unique_posts", "dtype_hint": "int", "description": "Unique posts collected (post_uri+cid)."},
        {"column_name": "num_unique_authors", "dtype_hint": "int", "description": "Unique authors observed in feed items."},
        {
            "column_name": "auth_profile_hydration_rate",
            "dtype_hint": "float",
            "description": "Hydrated authors / unique authors.",
        },
        {
            "column_name": "snapshot_success_rate",
            "dtype_hint": "float",
            "description": "Successful snapshots / expected snapshots.",
        },
        {"column_name": "mapping_notes", "dtype_hint": "str", "description": "High-level notes about data sources."},
    ],
    "errors.csv": [
        {"column_name": "stage", "dtype_hint": "str", "description": "Pipeline stage name."},
        {"column_name": "key", "dtype_hint": "str", "description": "Entity key (e.g., feed_uri, starterpack_uri)."},
        {"column_name": "error_type", "dtype_hint": "str", "description": "Error classification."},
        {"column_name": "http_status", "dtype_hint": "int", "description": "HTTP status code when applicable."},
        {"column_name": "error_message", "dtype_hint": "str", "description": "Short error message."},
        {"column_name": "when_utc", "dtype_hint": "str", "description": "Error time (UTC, ISO-8601)."},
        {"column_name": "retry_count", "dtype_hint": "int", "description": "Retry attempt count at time of error."},
    ],
    "feed_generators_index.csv": [
        {"column_name": "feed_uri", "dtype_hint": "str", "description": "Feed generator at-uri."},
        {"column_name": "creator_did", "dtype_hint": "str", "description": "Creator DID (repo DID)."},
        {"column_name": "rkey", "dtype_hint": "str", "description": "Record key component of at-uri."},
        {"column_name": "service_did", "dtype_hint": "str", "description": "Service DID hosting the generator."},
        {"column_name": "provider_bucket", "dtype_hint": "str", "description": "did:web domain or plc_bucket."},
        {"column_name": "display_name", "dtype_hint": "str", "description": "Display name (if present)."},
        {"column_name": "description", "dtype_hint": "str", "description": "Description (if present)."},
        {"column_name": "accepts_interaction", "dtype_hint": "int", "description": "Whether interactions are accepted (if present)."},
        {"column_name": "content_mode", "dtype_hint": "str", "description": "Content mode (if present)."},
        {"column_name": "indexed_at", "dtype_hint": "str", "description": "IndexedAt timestamp (if present)."},
    ],
    "starterpacks.csv": [
        {"column_name": "starterpack_uri", "dtype_hint": "str", "description": "Starter pack at-uri."},
        {"column_name": "creator_did", "dtype_hint": "str", "description": "Creator DID (if present)."},
        {"column_name": "name", "dtype_hint": "str", "description": "Starter pack name (if present)."},
        {"column_name": "description", "dtype_hint": "str", "description": "Starter pack description (if present)."},
        {"column_name": "collected_at_utc", "dtype_hint": "str", "description": "Collection time (UTC, ISO-8601)."},
    ],
    "starterpack_feeds.csv": [
        {"column_name": "starterpack_uri", "dtype_hint": "str", "description": "Starter pack at-uri."},
        {"column_name": "slot_index", "dtype_hint": "int", "description": "0-based feed slot index within the pack."},
        {"column_name": "feed_uri", "dtype_hint": "str", "description": "Feed generator at-uri."},
    ],
    "discovery_feed_inclusions.csv": [
        {"column_name": "feed_uri", "dtype_hint": "str", "description": "Feed generator at-uri."},
        {"column_name": "inclusion_count", "dtype_hint": "int", "description": "Number of packs containing the feed."},
        {"column_name": "slot_count", "dtype_hint": "int", "description": "Total appearances across all packs."},
        {"column_name": "inclusion_rank", "dtype_hint": "int", "description": "Rank by slot_count desc."},
    ],
    "popular_feeds.csv": [
        {"column_name": "feed_uri", "dtype_hint": "str", "description": "Feed generator at-uri."},
        {"column_name": "popularity_rank", "dtype_hint": "int", "description": "Rank order returned by popular endpoint (first seen)."},
        {"column_name": "collected_at_utc", "dtype_hint": "str", "description": "Collection time (UTC, ISO-8601)."},
    ],
    "feed_panel.csv": [
        {"column_name": "feed_uri", "dtype_hint": "str", "description": "Feed generator at-uri."},
        {"column_name": "feed_group", "dtype_hint": "str", "description": "discovery_surfaced|popular|less_known."},
        {"column_name": "selection_reason", "dtype_hint": "str", "description": "Why this feed was selected."},
        {"column_name": "provider_bucket", "dtype_hint": "str", "description": "did:web domain or plc_bucket."},
        {"column_name": "service_did", "dtype_hint": "str", "description": "Service DID hosting the generator."},
        {"column_name": "creator_did", "dtype_hint": "str", "description": "Creator DID."},
        {"column_name": "display_name", "dtype_hint": "str", "description": "Feed display name."},
        {"column_name": "inclusion_count", "dtype_hint": "int", "description": "Starter pack inclusion_count if known."},
        {"column_name": "popularity_rank", "dtype_hint": "int", "description": "Popularity rank if known."},
    ],
    "feed_snapshot_status.csv": [
        {"column_name": "feed_uri", "dtype_hint": "str", "description": "Feed generator at-uri."},
        {"column_name": "feed_group", "dtype_hint": "str", "description": "Feed group label."},
        {"column_name": "viewer_mode", "dtype_hint": "str", "description": "unauth|auth."},
        {"column_name": "collected_at_utc", "dtype_hint": "str", "description": "Snapshot time (UTC, ISO-8601)."},
        {"column_name": "requested_items", "dtype_hint": "int", "description": "Requested items per feed (config)."},
        {"column_name": "returned_items", "dtype_hint": "int", "description": "Items returned by API."},
        {"column_name": "pages_fetched", "dtype_hint": "int", "description": "Pagination pages fetched."},
        {"column_name": "success", "dtype_hint": "int", "description": "1 if snapshot succeeded else 0."},
        {"column_name": "http_status", "dtype_hint": "int", "description": "HTTP status code when failure is HTTP."},
        {"column_name": "error_type", "dtype_hint": "str", "description": "Error classification."},
        {"column_name": "error_message_short", "dtype_hint": "str", "description": "Short error message."},
    ],
    "feed_items.csv.gz": [
        {"column_name": "feed_uri", "dtype_hint": "str", "description": "Feed generator at-uri."},
        {"column_name": "feed_group", "dtype_hint": "str", "description": "Feed group label."},
        {"column_name": "viewer_mode", "dtype_hint": "str", "description": "unauth|auth."},
        {"column_name": "collected_at_utc", "dtype_hint": "str", "description": "Snapshot time (UTC, ISO-8601)."},
        {"column_name": "rank", "dtype_hint": "int", "description": "1-based rank within feed snapshot."},
        {"column_name": "post_uri", "dtype_hint": "str", "description": "Post at-uri."},
        {"column_name": "post_cid", "dtype_hint": "str", "description": "Post CID."},
        {"column_name": "author_did", "dtype_hint": "str", "description": "Author DID."},
        {"column_name": "author_handle", "dtype_hint": "str", "description": "Author handle if present."},
        {"column_name": "reason_type", "dtype_hint": "str", "description": "Feed item reason type (if present)."},
        {"column_name": "reason_actor_did", "dtype_hint": "str", "description": "Reason actor DID (if present)."},
    ],
    "posts.csv.gz": [
        {"column_name": "post_uri", "dtype_hint": "str", "description": "Post at-uri."},
        {"column_name": "post_cid", "dtype_hint": "str", "description": "Post CID."},
        {"column_name": "author_did", "dtype_hint": "str", "description": "Author DID."},
        {"column_name": "author_handle", "dtype_hint": "str", "description": "Author handle if present."},
        {"column_name": "record_created_at", "dtype_hint": "str", "description": "Record createdAt timestamp."},
        {"column_name": "indexed_at", "dtype_hint": "str", "description": "IndexedAt timestamp (if present)."},
        {"column_name": "text", "dtype_hint": "str", "description": "Post text."},
        {"column_name": "text_len", "dtype_hint": "int", "description": "Length of text in characters."},
        {"column_name": "is_reply", "dtype_hint": "int", "description": "1 if reply else 0."},
        {"column_name": "reply_parent_uri", "dtype_hint": "str", "description": "Parent post uri if reply."},
        {"column_name": "reply_root_uri", "dtype_hint": "str", "description": "Root post uri if reply."},
        {"column_name": "is_quote", "dtype_hint": "int", "description": "1 if quote embed else 0."},
        {"column_name": "quoted_uri", "dtype_hint": "str", "description": "Quoted post uri if quote."},
        {"column_name": "embed_type", "dtype_hint": "str", "description": "Embed type string (if present)."},
        {"column_name": "image_count", "dtype_hint": "int", "description": "Number of images in embed if any."},
        {"column_name": "external_uri", "dtype_hint": "str", "description": "External embed URL if any."},
        {"column_name": "external_domain", "dtype_hint": "str", "description": "Domain of external_uri if any."},
        {"column_name": "facet_link_count", "dtype_hint": "int", "description": "Number of link facets."},
        {"column_name": "link_domains_json", "dtype_hint": "str", "description": "JSON array of link domains from facets."},
        {"column_name": "mention_count", "dtype_hint": "int", "description": "Number of mention facets."},
        {"column_name": "hashtag_count", "dtype_hint": "int", "description": "Number of hashtag facets."},
        {"column_name": "like_count", "dtype_hint": "int", "description": "Like count if present."},
        {"column_name": "repost_count", "dtype_hint": "int", "description": "Repost count if present."},
        {"column_name": "reply_count", "dtype_hint": "int", "description": "Reply count if present."},
        {"column_name": "quote_count", "dtype_hint": "int", "description": "Quote count if present."},
        {"column_name": "langs_json", "dtype_hint": "str", "description": "JSON array of detected languages if present."},
        {"column_name": "post_labels_json", "dtype_hint": "str", "description": "JSON array of post labels if present."},
        {"column_name": "author_labels_json", "dtype_hint": "str", "description": "JSON array of author labels if present."},
    ],
    "post_labels.csv.gz": [
        {"column_name": "post_uri", "dtype_hint": "str", "description": "Post at-uri."},
        {"column_name": "post_cid", "dtype_hint": "str", "description": "Post CID."},
        {"column_name": "feed_uri", "dtype_hint": "str", "description": "Feed generator at-uri."},
        {"column_name": "viewer_mode", "dtype_hint": "str", "description": "unauth|auth."},
        {"column_name": "collected_at_utc", "dtype_hint": "str", "description": "Snapshot time (UTC, ISO-8601)."},
        {"column_name": "label_src", "dtype_hint": "str", "description": "Label source."},
        {"column_name": "label_val", "dtype_hint": "str", "description": "Label value."},
        {"column_name": "label_neg", "dtype_hint": "int", "description": "Label negation flag if present."},
        {"column_name": "label_uri", "dtype_hint": "str", "description": "Label URI if present."},
    ],
    "authors.csv.gz": [
        {"column_name": "author_did", "dtype_hint": "str", "description": "Author DID."},
        {"column_name": "handle", "dtype_hint": "str", "description": "Handle (if present)."},
        {"column_name": "display_name", "dtype_hint": "str", "description": "Display name (if present)."},
        {"column_name": "followers_count", "dtype_hint": "int", "description": "Followers count (if present)."},
        {"column_name": "follows_count", "dtype_hint": "int", "description": "Follows count (if present)."},
        {"column_name": "posts_count", "dtype_hint": "int", "description": "Posts count (if present)."},
        {"column_name": "collected_at_utc", "dtype_hint": "str", "description": "Profile hydration time (UTC, ISO-8601)."},
    ],
    "provider_stats.csv": [
        {"column_name": "provider_bucket", "dtype_hint": "str", "description": "did:web domain or plc_bucket."},
        {"column_name": "hosted_feed_count_api", "dtype_hint": "int", "description": "Feeds hosted by this provider (API index)."},
        {"column_name": "discovery_slot_count", "dtype_hint": "int", "description": "Total starter pack feed slots attributed to provider."},
        {"column_name": "hosting_share", "dtype_hint": "float", "description": "hosted_feed_count_api / total_indexed_feeds."},
        {"column_name": "discovery_share", "dtype_hint": "float", "description": "discovery_slot_count / total_discovery_slots."},
        {"column_name": "leverage_ratio", "dtype_hint": "float", "description": "discovery_share / hosting_share."},
    ],
    "validation_report.csv": [
        {"column_name": "check_name", "dtype_hint": "str", "description": "Validation check name."},
        {"column_name": "status", "dtype_hint": "str", "description": "PASS|FAIL."},
        {"column_name": "observed_value", "dtype_hint": "str", "description": "Observed value (stringified)."},
        {"column_name": "expected_threshold", "dtype_hint": "str", "description": "Expected threshold or condition."},
        {"column_name": "notes", "dtype_hint": "str", "description": "Extra notes or remediation hints."},
    ],
    "manifest.csv": [
        {"column_name": "file_name", "dtype_hint": "str", "description": "CSV file name under OUT_DIR/csv/."},
        {"column_name": "bytes", "dtype_hint": "int", "description": "File size in bytes."},
        {"column_name": "sha256", "dtype_hint": "str", "description": "SHA-256 hash of file contents."},
        {"column_name": "created_at_utc", "dtype_hint": "str", "description": "Manifest generation time (UTC, ISO-8601)."},
    ],
    "http_stats.csv": [
        {"column_name": "endpoint_name", "dtype_hint": "str", "description": "Logical endpoint name."},
        {"column_name": "request_count", "dtype_hint": "int", "description": "Total requests made."},
        {"column_name": "success_count", "dtype_hint": "int", "description": "2xx responses."},
        {"column_name": "rate_limited_count", "dtype_hint": "int", "description": "429 responses."},
        {"column_name": "avg_latency_ms", "dtype_hint": "float", "description": "Average latency (ms)."},
        {"column_name": "p95_latency_ms", "dtype_hint": "float", "description": "Approximate p95 latency (ms) from histogram."},
    ],
}
