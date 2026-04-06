from __future__ import annotations

import csv
from pathlib import Path

from bsky_collector_v2.appearance_file_index import iter_matching_feed_item_rows
from bsky_collector_v2.layout import Layout


def _write_feed_items_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_appearance_index_refreshes_changed_source(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    raw_path = layout.hourly_root / "2026-03-31" / "00" / "parts" / "feed_items_part_000.csv"
    post_uri_a = "at://did:plc:author000/app.bsky.feed.post/post000"
    post_uri_b = "at://did:plc:author001/app.bsky.feed.post/post001"

    _write_feed_items_csv(
        raw_path,
        [{"post_uri": post_uri_a, "surface_id": "raw-a"}],
    )

    initial_matches = list(iter_matching_feed_item_rows(layout, {post_uri_a}))
    assert len(initial_matches) == 1
    assert initial_matches[0].source_family == "hourly"
    assert initial_matches[0].source_path == raw_path
    assert initial_matches[0].row["surface_id"] == "raw-a"

    _write_feed_items_csv(
        raw_path,
        [{"post_uri": post_uri_b, "surface_id": "raw-b-updated"}],
    )

    stale_matches = list(iter_matching_feed_item_rows(layout, {post_uri_a}))
    updated_matches = list(iter_matching_feed_item_rows(layout, {post_uri_b}))
    assert stale_matches == []
    assert len(updated_matches) == 1
    assert updated_matches[0].row["surface_id"] == "raw-b-updated"


def test_appearance_index_prefers_effective_csv_over_raw_parts(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    hourly_post_uri = "at://did:plc:author100/app.bsky.feed.post/post100"
    wide_post_uri = "at://did:plc:author200/app.bsky.feed.post/post200"

    raw_hourly_path = layout.hourly_root / "2026-03-31" / "00" / "parts" / "feed_items_part_000.csv"
    effective_hourly_path = (
        layout.effective_timeseries_root / "hourly" / "2026-03-31" / "00" / "feed_items.csv"
    )
    raw_wide_path = layout.wide_root / "2026-03-31" / "parts" / "feed_items_part_000.csv"

    _write_feed_items_csv(
        raw_hourly_path,
        [{"post_uri": hourly_post_uri, "surface_id": "raw-hourly"}],
    )
    _write_feed_items_csv(
        effective_hourly_path,
        [{"post_uri": hourly_post_uri, "surface_id": "effective-hourly"}],
    )
    _write_feed_items_csv(
        raw_wide_path,
        [{"post_uri": wide_post_uri, "surface_id": "raw-wide"}],
    )

    matches = list(iter_matching_feed_item_rows(layout, {hourly_post_uri, wide_post_uri}))
    assert len(matches) == 2

    by_post_uri = {match.row["post_uri"]: match for match in matches}
    assert by_post_uri[hourly_post_uri].source_path == effective_hourly_path
    assert by_post_uri[hourly_post_uri].row["surface_id"] == "effective-hourly"
    assert by_post_uri[hourly_post_uri].source_family == "hourly"

    assert by_post_uri[wide_post_uri].source_path == raw_wide_path
    assert by_post_uri[wide_post_uri].row["surface_id"] == "raw-wide"
    assert by_post_uri[wide_post_uri].source_family == "wide"
