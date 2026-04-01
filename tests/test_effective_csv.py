from __future__ import annotations

import csv
import json
from pathlib import Path

from bsky_collector_v2.effective_csv import refresh_key_views, sync_effective_csv_full, sync_metadata_day
from bsky_collector_v2.layout import Layout


def _write_csv(path: Path, *, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _read_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as f:
        return sum(1 for _row in csv.DictReader(f))


def test_sync_effective_csv_full_filters_empty_and_merges_parts(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    layout = Layout(out_base=out_base)

    day = "2026-02-15"
    hour = "16"

    _write_csv(
        layout.metadata_day(day) / "feed_catalog.csv",
        fieldnames=["feed_uri", "like_count_last"],
        rows=[{"feed_uri": "at://did:plc:feed001/app.bsky.feed.generator/main", "like_count_last": "10"}],
    )
    _write_csv(
        layout.metadata_day(day) / "starterpack_feeds.csv",
        fieldnames=["pack_uri", "feed_uri"],
        rows=[],
    )
    _write_csv(
        layout.metadata_day(day) / "suggested_feeds.csv",
        fieldnames=["feed_uri", "position"],
        rows=[{"feed_uri": "at://did:plc:feed001/app.bsky.feed.generator/main", "position": "0"}],
    )

    _write_csv(
        layout.panel_version_csv(day),
        fieldnames=["feed_uri", "bucket", "unauth_skip", "built_at_utc", "panel_version_id"],
        rows=[
            {
                "feed_uri": "at://did:plc:feed001/app.bsky.feed.generator/main",
                "bucket": "popular_by_likecount",
                "unauth_skip": "0",
                "built_at_utc": "2026-02-15T16:00:00Z",
                "panel_version_id": day,
            }
        ],
    )
    _write_csv(
        layout.panel_active_csv,
        fieldnames=["feed_uri", "bucket", "unauth_skip", "built_at_utc", "panel_version_id"],
        rows=[
            {
                "feed_uri": "at://did:plc:feed001/app.bsky.feed.generator/main",
                "bucket": "popular_by_likecount",
                "unauth_skip": "0",
                "built_at_utc": "2026-02-15T16:00:00Z",
                "panel_version_id": day,
            }
        ],
    )

    hourly_parts = layout.hourly_root / day / hour / "parts"
    _write_csv(
        hourly_parts / "feed_items_part_000.csv",
        fieldnames=["run_id", "feed_uri", "rank"],
        rows=[{"run_id": "r1", "feed_uri": "at://feed/1", "rank": "1"}],
    )
    _write_csv(
        hourly_parts / "feed_items_part_001.csv",
        fieldnames=["run_id", "feed_uri", "rank"],
        rows=[],
    )
    _write_csv(
        hourly_parts / "post_labels_part_000.csv",
        fieldnames=["run_id", "post_uri", "label_val"],
        rows=[],
    )

    wide_parts = layout.wide_parts_dir(day)
    _write_csv(
        wide_parts / "post_metrics_part_000.csv",
        fieldnames=["run_id", "post_uri", "like_count"],
        rows=[{"run_id": "r1", "post_uri": "at://post/1", "like_count": "3"}],
    )
    _write_csv(
        wide_parts / "feed_items_part_000.csv",
        fieldnames=["run_id", "feed_uri", "rank"],
        rows=[{"run_id": "r1", "feed_uri": "at://feed/2", "rank": "1"}],
    )

    _write_csv(
        layout.authors_day_dir(day) / "author_profiles_part_000.csv",
        fieldnames=["run_id", "author_did"],
        rows=[{"run_id": "r1", "author_did": "did:plc:author001"}],
    )

    sync_effective_csv_full(layout)

    assert (layout.effective_timeseries_root / "metadata" / day / "feed_catalog.csv").exists()
    assert not (layout.effective_timeseries_root / "metadata" / day / "starterpack_feeds.csv").exists()

    hourly_merged = layout.effective_timeseries_root / "hourly" / day / hour / "feed_items.csv"
    assert hourly_merged.exists()
    assert _read_row_count(hourly_merged) == 1
    assert not (layout.effective_timeseries_root / "hourly" / day / hour / "post_labels.csv").exists()

    assert (layout.effective_timeseries_root / "wide" / day / "feed_items.csv").exists()
    assert (layout.effective_timeseries_root / "authors" / day / "author_profiles.csv").exists()

    assert (layout.effective_key_root / "metadata" / "feed_catalog.csv").exists()
    assert (layout.effective_key_root / "hourly" / "feed_items.csv").exists()
    assert (layout.effective_key_root / "panel" / "panel_v1.csv").exists()
    assert (layout.effective_key_root / "authors" / "author_profiles.csv").exists()

    sources = json.loads(layout.effective_key_sources_json.read_text(encoding="utf-8"))
    assert isinstance(sources.get("sources"), dict)
    assert "metadata/feed_catalog.csv" in sources["sources"]


def test_refresh_key_views_uses_latest_non_empty_metadata(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    layout = Layout(out_base=out_base)

    day1 = "2026-02-14"
    day2 = "2026-02-15"

    _write_csv(
        layout.metadata_day(day1) / "feed_catalog.csv",
        fieldnames=["feed_uri", "like_count_last"],
        rows=[{"feed_uri": "at://feed/day1", "like_count_last": "1"}],
    )
    _write_csv(
        layout.metadata_day(day2) / "feed_catalog.csv",
        fieldnames=["feed_uri", "like_count_last"],
        rows=[],
    )

    sync_metadata_day(layout, date_yyyy_mm_dd=day1)
    sync_metadata_day(layout, date_yyyy_mm_dd=day2)
    refresh_key_views(layout)

    key_feed_catalog = layout.effective_key_root / "metadata" / "feed_catalog.csv"
    assert key_feed_catalog.exists()
    with key_feed_catalog.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["feed_uri"] == "at://feed/day1"


def test_sync_effective_csv_full_merges_micro_window_parts(tmp_path: Path) -> None:
    out_base = tmp_path / "out"
    out_base.mkdir()
    layout = Layout(out_base=out_base)

    _write_csv(
        layout.micro5_parts_dir(
            study_id="study_micro",
            sample_family="micro5_core_full",
            date_yyyy_mm_dd="2026-03-17",
            hour_str="12",
            minute_str="10",
        )
        / "feed_items_part_000.csv",
        fieldnames=["run_id", "feed_uri", "rank"],
        rows=[{"run_id": "r1", "feed_uri": "at://feed/1", "rank": "1"}],
    )
    _write_csv(
        layout.micro5_parts_dir(
            study_id="study_micro",
            sample_family="micro5_core_full",
            date_yyyy_mm_dd="2026-03-17",
            hour_str="12",
            minute_str="10",
        )
        / "posts_first_seen_part_000.csv",
        fieldnames=["run_id", "post_uri", "text"],
        rows=[{"run_id": "r1", "post_uri": "at://post/1", "text": "hello"}],
    )
    _write_csv(
        layout.micro5_parts_dir(
            study_id="study_micro",
            sample_family="micro5_core_full",
            date_yyyy_mm_dd="2026-03-17",
            hour_str="12",
            minute_str="10",
        )
        / "post_metrics_part_000.csv",
        fieldnames=["run_id", "post_uri", "like_count"],
        rows=[{"run_id": "r1", "post_uri": "at://post/1", "like_count": "7"}],
    )
    _write_csv(
        layout.micro5_parts_dir(
            study_id="study_micro",
            sample_family="micro5_core_full",
            date_yyyy_mm_dd="2026-03-17",
            hour_str="12",
            minute_str="10",
        )
        / "post_labels_part_000.csv",
        fieldnames=["run_id", "post_uri", "label_val"],
        rows=[],
    )

    sync_effective_csv_full(layout)

    dest_dir = layout.effective_micro5_window_dir(
        study_id="study_micro",
        sample_family="micro5_core_full",
        date_yyyy_mm_dd="2026-03-17",
        hour_str="12",
        minute_str="10",
    )
    assert (dest_dir / "feed_items.csv").exists()
    assert _read_row_count(dest_dir / "feed_items.csv") == 1
    assert (dest_dir / "posts_first_seen.csv").exists()
    assert (dest_dir / "post_metrics.csv").exists()
    assert not (dest_dir / "post_labels.csv").exists()
