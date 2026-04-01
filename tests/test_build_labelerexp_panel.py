from __future__ import annotations

import csv
from pathlib import Path

from bsky_collector_v2.jobs.build_labelerexp_panel import LabelerExpPanelConfig, run_build_labelerexp_panel
from bsky_collector_v2.layout import Layout
from bsky_collector_v2.time_utils import now_utc, utc_date_str


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow(row)


def test_build_labelerexp_panel_uses_latest_suggested_snapshot_and_unauth_skip(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src_layout = Layout(out_base=src)
    dst_layout = Layout(out_base=dst)

    # Latest metadata day exists but is missing suggested_feeds.csv; builder should fall back.
    (src_layout.metadata_day("2026-02-21")).mkdir(parents=True, exist_ok=True)

    suggested = src_layout.suggested_feeds_csv("2026-02-20")
    _write_csv(
        suggested,
        header=["feed_uri", "position", "captured_at_utc", "vantage_id"],
        rows=[
            # Older snapshot.
            ["at://did:plc:old/app.bsky.feed.generator/a", "1", "2026-02-20T00:00:00Z", "unauth"],
            ["at://did:plc:old/app.bsky.feed.generator/b", "2", "2026-02-20T00:00:00Z", "unauth"],
            # Latest snapshot (should win).
            ["at://did:plc:new/app.bsky.feed.generator/c", "2", "2026-02-20T01:00:00Z", "unauth"],
            ["at://did:plc:new/app.bsky.feed.generator/d", "1", "2026-02-20T01:00:00Z", "unauth"],
        ],
    )

    _write_csv(
        src_layout.panel_active_csv,
        header=["feed_uri", "bucket", "unauth_skip", "built_at_utc", "panel_version_id"],
        rows=[
            ["at://did:plc:new/app.bsky.feed.generator/d", "suggested", "1", "2026-02-20T01:02:03Z", "2026-02-20"],
        ],
    )

    run_build_labelerexp_panel(
        layout=dst_layout,
        source_out_base=src,
        source_metadata_day=None,
        dry_run=False,
        cfg=LabelerExpPanelConfig(bucket="suggested_labelerexp"),
    )

    assert dst_layout.panel_active_csv.exists()
    rows = list(csv.DictReader(dst_layout.panel_active_csv.open("r", encoding="utf-8", newline="")))
    assert [r["feed_uri"] for r in rows] == [
        "at://did:plc:new/app.bsky.feed.generator/d",  # position=1
        "at://did:plc:new/app.bsky.feed.generator/c",  # position=2
    ]
    assert [r["bucket"] for r in rows] == ["suggested_labelerexp", "suggested_labelerexp"]
    # One feed inherits unauth_skip=1 from source panel; the other defaults to 0.
    assert [int(r["unauth_skip"]) for r in rows] == [1, 0]

    panel_version_id = utc_date_str(now_utc())
    assert dst_layout.panel_version_csv(panel_version_id).exists()


def test_build_labelerexp_panel_respects_max_feeds(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src_layout = Layout(out_base=src)
    dst_layout = Layout(out_base=dst)

    suggested = src_layout.suggested_feeds_csv("2026-02-20")
    _write_csv(
        suggested,
        header=["feed_uri", "position", "captured_at_utc", "vantage_id"],
        rows=[
            ["at://did:plc:x/app.bsky.feed.generator/1", "1", "2026-02-20T00:00:00Z", "unauth"],
            ["at://did:plc:x/app.bsky.feed.generator/2", "2", "2026-02-20T00:00:00Z", "unauth"],
            ["at://did:plc:x/app.bsky.feed.generator/3", "3", "2026-02-20T00:00:00Z", "unauth"],
        ],
    )

    run_build_labelerexp_panel(
        layout=dst_layout,
        source_out_base=src,
        source_metadata_day="2026-02-20",
        dry_run=False,
        cfg=LabelerExpPanelConfig(bucket="suggested", max_feeds=2),
    )

    rows = list(csv.DictReader(dst_layout.panel_active_csv.open("r", encoding="utf-8", newline="")))
    assert len(rows) == 2

