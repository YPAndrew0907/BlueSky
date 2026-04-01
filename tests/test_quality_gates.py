from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from bsky_collector_v2.layout import Layout
from bsky_collector_v2.quality import assess_snapshot_hour
from bsky_collector_v2.time_utils import SnapshotHour


def _write_csv(path: Path, *, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_snapshot_quality_quarantines_when_failure_ratio_too_high(tmp_path: Path) -> None:
    layout = Layout(out_base=tmp_path / "out")
    hour = SnapshotHour(hour_utc=datetime.fromisoformat("2026-03-11T00:00:00+00:00"))
    hour_dir = layout.hourly_hour_dir(hour)
    hour_dir.mkdir(parents=True, exist_ok=True)

    (hour_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "job_name": "snapshot-panel",
                "sample_family": "regular_hourly",
                "collection_params_hash": "abc",
                "snapshot_hour_utc": hour.hour_iso_z,
                "started_at_utc": "2026-03-11T00:00:00Z",
                "success": True,
                "params": {
                    "posts_per_feed": 50,
                    "time_budget_minutes": 55,
                    "feed_time_budget_s": 20.0,
                    "accept_language": "en-US",
                },
            }
        ),
        encoding="utf-8",
    )
    (hour_dir / "progress.json").write_text(
        json.dumps({"feeds_total": 10, "feeds_done": 1, "feeds_failed": 9}),
        encoding="utf-8",
    )
    _write_csv(
        hour_dir / "request_provenance.csv",
        fieldnames=["run_id", "job_name", "sample_family", "collection_params_hash", "request_started_at_utc"],
        rows=[{"run_id": "r1", "job_name": "snapshot-panel", "sample_family": "regular_hourly", "collection_params_hash": "abc", "request_started_at_utc": "2026-03-11T00:00:00Z"}],
    )
    _write_csv(
        hour_dir / "parts" / "feed_items_part_000.csv",
        fieldnames=["run_id", "snapshot_hour_utc", "captured_at_utc", "viewer_mode", "vantage_id", "feed_uri", "rank"],
        rows=[{"run_id": "r1", "snapshot_hour_utc": hour.hour_iso_z, "captured_at_utc": "2026-03-11T00:00:00Z", "viewer_mode": "unauth", "vantage_id": "unauth_enUS", "feed_uri": "at://feed/1", "rank": "1"}],
    )
    (layout.metadata_day(hour.date_str) / "discovery_status.json").parent.mkdir(parents=True, exist_ok=True)
    (layout.metadata_day(hour.date_str) / "discovery_status.json").write_text(json.dumps({"success": True}), encoding="utf-8")
    _write_csv(
        layout.feed_catalog_csv(hour.date_str),
        fieldnames=["feed_uri", "like_count_last"],
        rows=[{"feed_uri": "at://feed/1", "like_count_last": "1"}],
    )

    report = assess_snapshot_hour(layout, hour=hour)
    codes = {issue["code"] for issue in report["issues"]}
    assert report["verdict"] == "quarantined"
    assert "failure_ratio_exceeds_threshold" in codes


def test_labelerexp_snapshot_quality_falls_back_to_primary_metadata(tmp_path: Path) -> None:
    primary = Layout(out_base=tmp_path / "data_v2_full")
    labelerexp = Layout(out_base=primary.out_base / "labelerexp")
    hour = SnapshotHour(hour_utc=datetime.fromisoformat("2026-02-21T00:00:00+00:00"))
    hour_dir = labelerexp.hourly_hour_dir(hour)
    hour_dir.mkdir(parents=True, exist_ok=True)

    (hour_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "job_name": "snapshot-panel",
                "sample_family": "experimental_labelerexp_hourly",
                "collection_params_hash": "abc",
                "snapshot_hour_utc": hour.hour_iso_z,
                "started_at_utc": "2026-02-21T00:20:00Z",
                "success": True,
                "params": {
                    "posts_per_feed": 50,
                    "time_budget_minutes": 55,
                    "feed_time_budget_s": 20.0,
                    "accept_language": "en-US",
                    "accept_labelers": "did:plc:test",
                },
            }
        ),
        encoding="utf-8",
    )
    (hour_dir / "progress.json").write_text(
        json.dumps({"feeds_total": 1, "feeds_done": 1, "feeds_failed": 0}),
        encoding="utf-8",
    )
    _write_csv(
        hour_dir / "request_provenance.csv",
        fieldnames=["run_id", "job_name", "sample_family", "collection_params_hash", "request_started_at_utc"],
        rows=[{"run_id": "r1", "job_name": "snapshot-panel", "sample_family": "experimental_labelerexp_hourly", "collection_params_hash": "abc", "request_started_at_utc": "2026-02-21T00:20:00Z"}],
    )
    _write_csv(
        hour_dir / "parts" / "feed_items_part_000.csv",
        fieldnames=["run_id", "snapshot_hour_utc", "captured_at_utc", "viewer_mode", "vantage_id", "feed_uri", "rank"],
        rows=[{"run_id": "r1", "snapshot_hour_utc": hour.hour_iso_z, "captured_at_utc": "2026-02-21T00:20:00Z", "viewer_mode": "auth", "vantage_id": "auth_enUS_labelerexp", "feed_uri": "at://feed/1", "rank": "1"}],
    )

    (primary.metadata_day(hour.date_str) / "discovery_status.json").parent.mkdir(parents=True, exist_ok=True)
    (primary.metadata_day(hour.date_str) / "discovery_status.json").write_text(json.dumps({"success": True}), encoding="utf-8")
    _write_csv(
        primary.feed_catalog_csv(hour.date_str),
        fieldnames=["feed_uri", "like_count_last"],
        rows=[{"feed_uri": "at://feed/1", "like_count_last": "1"}],
    )

    report = assess_snapshot_hour(labelerexp, hour=hour)
    assert report["verdict"] == "promoted"
    assert report["metrics"]["metadata_source_out_base"] == str(primary.out_base)
