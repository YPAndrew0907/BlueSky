from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from bsky_collector_v2.jobs.backfill_run_artifacts import run_backfill_run_artifacts
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.time_utils import SnapshotHour


def _write_csv(path: Path, *, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_backfill_run_artifacts_retrofits_legacy_outputs(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    layout = Layout(out_base=out_base)

    day = "2026-02-26"
    hour = "00"
    snapshot_hour_utc = "2026-02-26T00:00:00Z"
    captured_at_utc = "2026-02-26T00:18:00Z"
    snapshot_hour = SnapshotHour(hour_utc=datetime.fromisoformat(snapshot_hour_utc.replace("Z", "+00:00")))

    _write_csv(
        layout.panel_version_csv(day),
        fieldnames=["feed_uri", "bucket", "unauth_skip", "built_at_utc", "panel_version_id"],
        rows=[
            {
                "feed_uri": "at://did:plc:feed001/app.bsky.feed.generator/main",
                "bucket": "popular_by_likecount",
                "unauth_skip": "0",
                "built_at_utc": captured_at_utc,
                "panel_version_id": day,
            }
        ],
    )

    layout.hourly_hour_dir(snapshot_hour).mkdir(parents=True, exist_ok=True)
    (layout.hourly_root / day / hour / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "snapshot-run-1",
                "job_name": "snapshot-panel",
                "snapshot_hour_utc": snapshot_hour_utc,
                "started_at_utc": captured_at_utc,
                "success": True,
                "params": {
                    "snapshot_hour_utc": snapshot_hour_utc,
                    "viewer_modes": ["unauth"],
                    "posts_per_feed": 50,
                    "time_budget_minutes": 55,
                    "feed_time_budget_s": 20.0,
                    "accept_language": "en-US",
                },
            }
        ),
        encoding="utf-8",
    )
    (layout.hourly_root / day / hour / "progress.json").write_text(
        json.dumps({"feeds_done": 1, "feeds_failed": 0}),
        encoding="utf-8",
    )
    _write_csv(
        layout.hourly_parts_dir(snapshot_hour) / "feed_items_part_000.csv",
        fieldnames=[
            "run_id",
            "snapshot_hour_utc",
            "captured_at_utc",
            "viewer_mode",
            "vantage_id",
            "feed_uri",
            "bucket",
            "rank",
            "post_uri",
            "post_cid",
            "author_did",
            "author_handle",
            "reason_type",
            "reason_actor_did",
        ],
        rows=[
            {
                "run_id": "snapshot-run-1",
                "snapshot_hour_utc": snapshot_hour_utc,
                "captured_at_utc": captured_at_utc,
                "viewer_mode": "unauth",
                "vantage_id": "unauth_enUS",
                "feed_uri": "at://did:plc:feed001/app.bsky.feed.generator/main",
                "bucket": "popular_by_likecount",
                "rank": "1",
                "post_uri": "at://did:plc:author/app.bsky.feed.post/1",
                "post_cid": "cid1",
                "author_did": "did:plc:author",
                "author_handle": "author.test",
                "reason_type": "",
                "reason_actor_did": "",
            }
        ],
    )
    _write_csv(
        layout.hourly_http_stats_csv(snapshot_hour),
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
        rows=[
            {
                "timestamp_utc": captured_at_utc,
                "endpoint": "app.bsky.feed.getFeed",
                "status_code": "200",
                "latency_ms": "10.0",
                "attempt": "0",
                "error_type": "",
                "feed_uri": "at://did:plc:feed001/app.bsky.feed.generator/main",
            }
        ],
    )

    (layout.wide_day_dir(day)).mkdir(parents=True, exist_ok=True)
    (layout.wide_manifest_json(day)).write_text(
        json.dumps(
            {
                "run_id": "wide-run-1",
                "job_name": "wide-sweep",
                "date_utc": day,
                "started_at_utc": f"{day}T02:00:00Z",
                "success": True,
                "params": {"date_utc": day, "posts_per_feed": 20, "accept_language": "en-US"},
            }
        ),
        encoding="utf-8",
    )
    (layout.wide_progress_json(day)).write_text(json.dumps({"feeds_done": 1, "feeds_failed": 0}), encoding="utf-8")
    _write_csv(
        layout.wide_parts_dir(day) / "feed_items_part_000.csv",
        fieldnames=[
            "run_id",
            "snapshot_hour_utc",
            "captured_at_utc",
            "viewer_mode",
            "vantage_id",
            "feed_uri",
            "bucket",
            "rank",
            "post_uri",
            "post_cid",
            "author_did",
            "author_handle",
            "reason_type",
            "reason_actor_did",
        ],
        rows=[
            {
                "run_id": "wide-run-1",
                "snapshot_hour_utc": snapshot_hour_utc,
                "captured_at_utc": captured_at_utc,
                "viewer_mode": "unauth",
                "vantage_id": "unauth_enUS",
                "feed_uri": "at://did:plc:feed002/app.bsky.feed.generator/main",
                "bucket": "wide_sweep",
                "rank": "1",
                "post_uri": "at://did:plc:author/app.bsky.feed.post/2",
                "post_cid": "cid2",
                "author_did": "did:plc:author",
                "author_handle": "author.test",
                "reason_type": "",
                "reason_actor_did": "",
            }
        ],
    )
    _write_csv(
        layout.wide_http_stats_csv(day),
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
        rows=[
            {
                "timestamp_utc": captured_at_utc,
                "endpoint": "app.bsky.feed.getFeed",
                "status_code": "200",
                "latency_ms": "12.0",
                "attempt": "0",
                "error_type": "",
                "feed_uri": "at://did:plc:feed002/app.bsky.feed.generator/main",
            }
        ],
    )

    _write_csv(
        layout.feed_catalog_csv(day),
        fieldnames=["feed_uri", "like_count_last"],
        rows=[{"feed_uri": "at://did:plc:feed001/app.bsky.feed.generator/main", "like_count_last": "10"}],
    )
    (layout.metadata_discovery_status_json(day)).write_text(
        json.dumps(
            {
                "run_id": "discovery-run-1",
                "started_at_utc": f"{day}T00:15:00Z",
                "finished_at_utc": f"{day}T00:20:00Z",
                "success": True,
                "viewer_mode": "unauth",
                "vantage_id": "unauth_enUS",
                "surfaces": {"suggested_feeds": {"status": "success"}},
            }
        ),
        encoding="utf-8",
    )

    missing_status_day = "2026-03-12"
    _write_csv(
        layout.feed_catalog_csv(missing_status_day),
        fieldnames=["feed_uri", "like_count_last"],
        rows=[{"feed_uri": "at://did:plc:feed999/app.bsky.feed.generator/main", "like_count_last": "1"}],
    )

    _write_csv(
        layout.authors_day_dir(day) / "author_profiles_part_000.csv",
        fieldnames=[
            "run_id",
            "vantage_id",
            "author_did",
            "handle",
            "display_name",
            "followers_count",
            "follows_count",
            "posts_count",
            "captured_at_utc",
        ],
        rows=[
            {
                "run_id": "authors-run-1",
                "vantage_id": "unauth_enUS",
                "author_did": "did:plc:author",
                "handle": "author.test",
                "display_name": "Author",
                "followers_count": "1",
                "follows_count": "2",
                "posts_count": "3",
                "captured_at_utc": captured_at_utc,
            }
        ],
    )

    fg_day = layout.feed_generators_index_day_dir(day)
    fg_day.mkdir(parents=True, exist_ok=True)
    (fg_day / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "index-run-1",
                "job_name": "index-feed-generators",
                "date_utc": day,
                "started_at_utc": f"{day}T00:30:00Z",
                "success": True,
                "params": {
                    "date_utc": day,
                    "relay_host": "https://bsky.network",
                    "records_host": "https://bsky.social",
                },
            }
        ),
        encoding="utf-8",
    )
    (fg_day / "progress.json").write_text(json.dumps({"feeds_done": 1, "feeds_failed": 0}), encoding="utf-8")
    _write_csv(
        fg_day / "http_stats.csv",
        fieldnames=["timestamp_utc", "endpoint", "status_code", "latency_ms", "attempt", "error_type", "feed_uri"],
        rows=[
            {
                "timestamp_utc": captured_at_utc,
                "endpoint": "com.atproto.sync.listReposByCollection",
                "status_code": "200",
                "latency_ms": "5.0",
                "attempt": "0",
                "error_type": "",
                "feed_uri": "",
            }
        ],
    )

    summary = run_backfill_run_artifacts(layout=layout, dry_run=False)

    assert summary.snapshot_manifests_updated >= 1
    assert summary.snapshot_request_provenance_written >= 1
    assert summary.wide_request_provenance_written >= 1

    snapshot_manifest = json.loads((layout.hourly_root / day / hour / "run_manifest.json").read_text(encoding="utf-8"))
    assert snapshot_manifest["sample_family"] == "regular_hourly"
    assert snapshot_manifest["panel_version_id"] == day
    assert snapshot_manifest["collection_params_hash"]

    snapshot_request_rows = list(
        csv.DictReader((layout.hourly_root / day / hour / "request_provenance.csv").open("r", encoding="utf-8", newline=""))
    )
    assert len(snapshot_request_rows) == 1
    assert snapshot_request_rows[0]["provenance_source"] == "legacy_feed_items_plus_http_stats"
    assert snapshot_request_rows[0]["viewer_mode"] == "unauth"

    snapshot_quality = json.loads((layout.hourly_root / day / hour / "quality_report.json").read_text(encoding="utf-8"))
    assert snapshot_quality["verdict"] == "promoted"

    wide_quality = json.loads((layout.wide_day_dir(day) / "quality_report.json").read_text(encoding="utf-8"))
    assert wide_quality["verdict"] == "promoted"

    metadata_manifest = json.loads(layout.metadata_manifest_json(missing_status_day).read_text(encoding="utf-8"))
    assert metadata_manifest["sample_family"] == "discovery_metadata"

    metadata_quality = json.loads(layout.metadata_quality_report_json(missing_status_day).read_text(encoding="utf-8"))
    assert metadata_quality["verdict"] == "quarantined"

    authors_manifest = json.loads(layout.authors_manifest_json(day).read_text(encoding="utf-8"))
    assert authors_manifest["sample_family"] == "author_profile_hydration"
    authors_quality = json.loads(layout.authors_quality_report_json(day).read_text(encoding="utf-8"))
    assert authors_quality["verdict"] == "promoted"

    index_manifest = json.loads(layout.feed_generators_index_manifest_json(day).read_text(encoding="utf-8"))
    assert index_manifest["sample_family"] == "feed_generator_index"
    index_quality = json.loads(layout.feed_generators_index_quality_report_json(day).read_text(encoding="utf-8"))
    assert index_quality["verdict"] == "promoted"
